#!/usr/bin/env python3
"""トップページの地図の輪郭（日本と周辺の陸地）を SVG のパスにする（1 回だけ動かす）。

元はデスクトップアプリのアメダスの地図と同じ Natural Earth 10m の陸地
（src/aiseed_weather/figures/_japan_coastline.npz。約 500m に軽くしたもの）。
ページに直接埋め込むので、さらに約 2km（0.02°）に間引き、小さな島は落とす。
投影はアプリの地図と同じで、経度方向を cos 36° 倍した経緯度。

出力: assets/japan_outline.json  {"viewBox", "d", "lon0", "lat1", "scale", "kx"}
（ページのスクリプトは lon0・lat1・scale・kx で地点を同じ座標に置く）

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

LON0, LON1, LAT0, LAT1 = 122.5, 149.0, 24.0, 46.0
TOLERANCE = 0.02            # 度
MIN_SIZE = 0.06             # これより小さな島は落とす（度）
SCALE = 20.0                # 1 度あたりの単位
KX = math.cos(math.radians(36.0))


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


def main() -> int:
    with np.load(SRC) as d:
        land = d["land"]
    breaks = np.nonzero(np.isnan(land[:, 0]))[0]
    rings = [r[~np.isnan(r[:, 0])] for r in np.split(land, breaks)]
    parts = []
    for r in rings:
        if len(r) < 4 or max(np.ptp(r[:, 0]), np.ptp(r[:, 1])) < MIN_SIZE:
            continue
        r = simplify(r.astype(np.float64), TOLERANCE)
        if len(r) < 4:
            continue
        x = (r[:, 0] - LON0) * KX * SCALE
        y = (LAT1 - r[:, 1]) * SCALE
        parts.append("M" + "L".join(f"{a:.1f},{b:.1f}" for a, b in zip(x, y)) + "Z")
    w = (LON1 - LON0) * KX * SCALE
    h = (LAT1 - LAT0) * SCALE
    out = {"viewBox": f"0 0 {w:.0f} {h:.0f}", "d": "".join(parts),
           "lon0": LON0, "lat1": LAT1, "scale": SCALE, "kx": round(KX, 6),
           "source": "Natural Earth 10m land (public domain)"}
    OUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"{OUT.name}: {len(parts)} 個の陸地, {len(out['d']):,} 文字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
