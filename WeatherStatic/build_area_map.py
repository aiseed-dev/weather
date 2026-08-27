#!/usr/bin/env python3
"""アメダス地点 → 気象庁の予報区（office / area_code）の対応表を作る。

なぜ要るか
----------
R2 の置き場を point/{YYYYMMDD}/{area_code}/{アメダス番号}.json にするため、
全 1,286 地点に area_code が要る。しかし手元にある物には無い:

  amedastable.json … 緯度経度・標高・観測要素のみ。予報区は入っていない
  stations.json    … pref はあるが体系が違う（60 種。offices は 58 で、
                     北海道が 14 地方 vs 8 office と粒度が合わない）

名前で突き合わせると 60 種中 16 種が外れる。**手で対応表を書くと必ずずれる**ので、
気象庁の予報 JSON から逆引きする。予報 JSON には office ごとに「その区域の
アメダス地点別の気温予報」が入っており、**気象庁自身の区分**がそのまま得られる。

取得は 58 office ぶん（1 回だけ。地点の増減があったときに作り直す）。

使い方:
    python build_area_map.py            # master/area_map.json を作る
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from weatherlib import jma

BASE = Path(__file__).resolve().parent
MASTER = BASE / "master"
URL_AREA = "https://www.jma.go.jp/bosai/common/const/area.json"
# 一次細分区域（class10）の境界。offices の境界は公開されていないが、
# class10 の親を辿れば office に届く
URL_GEO = "https://www.jma.go.jp/bosai/common/const/geojson/class10s.json"


def log(msg: str) -> None:
    print(f"[area-map] {msg}", flush=True)


def _in_ring(x: float, y: float, ring: list) -> bool:
    """点が多角形の中か（交差数判定）。外部依存を増やさないため自前で持つ。"""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def _in_feature(x: float, y: float, geom: dict) -> bool:
    """MultiPolygon / Polygon に対する内外判定。穴（内側の環）も見る。"""
    polys = (geom["coordinates"] if geom["type"] == "MultiPolygon"
             else [geom["coordinates"]])
    for poly in polys:
        if not poly or not _in_ring(x, y, poly[0]):
            continue
        if any(_in_ring(x, y, hole) for hole in poly[1:]):
            continue                      # 穴の中なので外
        return True
    return False


def _bbox(geom: dict) -> tuple[float, float, float, float]:
    """粗い絞り込み用。全図形と総当たりすると 1,286 x 153 で遅い。"""
    xs, ys = [], []
    polys = (geom["coordinates"] if geom["type"] == "MultiPolygon"
             else [geom["coordinates"]])
    for poly in polys:
        for pt in poly[0]:
            xs.append(pt[0]); ys.append(pt[1])
    return min(xs), min(ys), max(xs), max(ys)


def main() -> int:
    area = json.loads(jma.http_get(URL_AREA))
    offices, class10s = area["offices"], area["class10s"]
    geo = json.loads(jma.http_get(URL_GEO))
    log(f"予報区 {len(offices)} / 一次細分区域 {len(class10s)} / 境界 {len(geo['features'])}")

    # class10 の図形に、親の office を添えて持つ
    shapes = []
    for f in geo["features"]:
        c10 = f["properties"].get("code")
        parent = (class10s.get(c10) or {}).get("parent")
        if parent in offices:
            shapes.append((parent, _bbox(f["geometry"]), f["geometry"]))
    log(f"office に辿れた境界 {len(shapes)}")

    stations = json.loads((MASTER / "stations.json").read_text(encoding="utf-8"))["stations"]
    area_of: dict[str, str] = {}
    missing = []
    for rec in stations.values():
        x, y = rec["lon"], rec["lat"]
        hit = None
        for office, (x0, y0, x1, y1), geom in shapes:
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                continue                  # 外接矩形で粗く落とす
            if _in_feature(x, y, geom):
                hit = office
                break
        if hit:
            area_of[rec["amedas"]] = hit
        else:
            missing.append((rec["amedas"], rec["name"], x, y))

    log(f"境界の内側で決まった地点 {len(area_of)} / 全 {len(stations)}")

    # 海岸線ぎりぎりの地点（岬・離島・埋立地）は、どの多角形にも入らない
    # ことがある。**捨てない** — 最も近い境界の予報区へ寄せる。
    # 距離は緯度経度の二乗和で足りる（比較にしか使わない）。
    nudged = []
    for code, name, x, y in missing:
        best, best_d = None, None
        for office, _bb, geom in shapes:
            polys = (geom["coordinates"] if geom["type"] == "MultiPolygon"
                     else [geom["coordinates"]])
            for poly in polys:
                for px, py in poly[0]:
                    d = (px - x) ** 2 + (py - y) ** 2
                    if best_d is None or d < best_d:
                        best_d, best = d, office
        area_of[code] = best
        nudged.append((code, name, best, best_d ** 0.5))
    if nudged:
        log(f"境界外だったので最近傍へ寄せた地点 {len(nudged)}")
        for c, n, o, d in nudged[:5]:
            log(f"  {c} {n} → {offices[o]['name']}（{d:.3f}度）")
        far = [x for x in nudged if x[3] > 0.5]
        if far:
            log(f"  **0.5 度以上離れている {len(far)} 地点**（要確認）: "
                + ", ".join(f"{c} {n}" for c, n, _o, _d in far[:5]))

    from collections import Counter
    per = Counter(area_of.values())
    log(f"予報区あたりの地点数: 最大 {max(per.values())} / 最小 {min(per.values())}")

    out = MASTER / "area_map.json"
    out.write_text(json.dumps(
        {"note": "アメダス番号 → 気象庁 office(予報区)。class10 の境界で座標判定",
         "offices": {k: v["name"] for k, v in offices.items()},
         "count": len(area_of), "nudged": {c: o for c, _n, o, _d in nudged},
         "stations": area_of},
        ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"master/area_map.json を出力（{out.stat().st_size/1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
