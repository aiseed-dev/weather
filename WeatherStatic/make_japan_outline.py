#!/usr/bin/env python3
"""トップページの地図の輪郭（日本と周辺の陸地）を SVG のパスにする（1 回だけ動かす）。

元はデスクトップアプリのアメダスの地図と同じ Natural Earth 10m の陸地
（src/aiseed_weather/figures/_japan_coastline.npz。約 500m に軽くしたもの）。
ページに直接埋め込むので、さらに約 2km（0.02°）に間引き、小さな島は落とす。
投影は経度方向を cos 36° 倍した経緯度（アプリの地図と同じ）。

本土（九州〜北海道）を大きく描くため、沖縄・奄美は右下の太平洋（空いている所）に
枠で囲んだ差し込み図として置く。テレビの天気図と同じ置き方。

出力: assets/japan_outline.json
  {"viewBox", "main": {"d", "lon0", "lat1", "scale"}, "kx",
   "inset": {"d", "lon0", "lat1", "scale", "x", "y", "w", "h", "lat_max", "lon_max"}}
  地点は lat < inset.lat_max かつ lon < inset.lon_max なら差し込み図の座標に置く
  （ページのスクリプト）。差し込み図のパスは置いた後の座標で、枠で切り抜いて描く。

使い方:
    python make_japan_outline.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
SRC = BASE.parent / "src" / "aiseed_weather" / "figures" / "_japan_coastline.npz"
OUT = BASE / "assets" / "japan_outline.json"

KX = math.cos(math.radians(36.0))
SCALE = 20.0                                   # 1 度あたりの単位（本土）
MAIN = (128.5, 148.0, 29.5, 45.8)              # lon0, lon1, lat0, lat1
INSET = (122.6, 130.4, 24.0, 28.9)             # 与那国〜奄美
INSET_SCALE = 18.0
INSET_SPLIT = (29.3, 131.5)                    # これより南・西の地点は差し込み図に
TOLERANCE = 0.02            # 度
MIN_SIZE = 0.06             # これより小さな島は落とす（度）


def simplify(pts: np.ndarray, tol: float) -> np.ndarray:
    """Douglas–Peucker（繰り返し版）。"""
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = pts[i], pts[j]
        seg = b - a
        n = np.hypot(*seg)
        rel = pts[i + 1:j] - a
        d = (np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / n) if n else np.hypot(rel[:, 0], rel[:, 1])
        k = int(np.argmax(d))
        if d[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack += [(i, m), (m, j)]
    return pts[keep]


def rings() -> list[np.ndarray]:
    with np.load(SRC) as d:
        land = d["land"]
    breaks = np.nonzero(np.isnan(land[:, 0]))[0]
    out = []
    for r in np.split(land, breaks):
        r = r[~np.isnan(r[:, 0])]
        if len(r) < 4 or max(np.ptp(r[:, 0]), np.ptp(r[:, 1])) < MIN_SIZE:
            continue
        r = simplify(r.astype(np.float64), TOLERANCE)
        if len(r) >= 4:
            out.append(r)
    return out


def path(parts: list[np.ndarray], frame, scale: float, dx: float = 0.0, dy: float = 0.0) -> str:
    lon0, lon1, lat0, lat1 = frame
    d = []
    for r in parts:
        if r[:, 0].max() < lon0 or r[:, 0].min() > lon1 or r[:, 1].max() < lat0 or r[:, 1].min() > lat1:
            continue
        x = dx + (r[:, 0] - lon0) * KX * scale
        y = dy + (lat1 - r[:, 1]) * scale
        d.append("M" + "L".join(f"{a:.1f},{b:.1f}" for a, b in zip(x, y)) + "Z")
    return "".join(d)


def main() -> int:
    parts = rings()
    w = (MAIN[1] - MAIN[0]) * KX * SCALE
    h = (MAIN[3] - MAIN[2]) * SCALE
    iw = (INSET[1] - INSET[0]) * KX * INSET_SCALE
    ih = (INSET[3] - INSET[2]) * INSET_SCALE
    ix, iy = w - iw - 6, h - ih - 6              # 右下の太平洋
    out = {
        "viewBox": f"0 0 {w:.0f} {h:.0f}", "kx": round(KX, 6),
        "main": {"d": path(parts, MAIN, SCALE), "lon0": MAIN[0], "lat1": MAIN[3], "scale": SCALE},
        "inset": {"d": path(parts, INSET, INSET_SCALE, ix, iy), "lon0": INSET[0], "lat1": INSET[3],
                  "scale": INSET_SCALE, "x": round(ix, 1), "y": round(iy, 1),
                  "w": round(iw, 1), "h": round(ih, 1),
                  "lat_max": INSET_SPLIT[0], "lon_max": INSET_SPLIT[1]},
        "source": "Natural Earth 10m land (public domain)",
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"{OUT.name}: 本土 {len(out['main']['d']):,} 文字・差し込み図 {len(out['inset']['d']):,} 文字"
          f"（{w:.0f}×{h:.0f}、差し込み図 {iw:.0f}×{ih:.0f} を ({ix:.0f}, {iy:.0f}) に）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
