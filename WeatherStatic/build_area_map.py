#!/usr/bin/env python3
"""アメダス地点 → area_code の対応表を作る。

正は **ame_master**（気象庁のアメダス公式マスター）。
現況配信（map JSON）と過不足なく 1,286 地点で一致することを確認済みなので、
「アメダスの Web サイトに合わせる」の基準はこれになる。

area_code の決め方
------------------
基本は ame_master の**都府県振興局**（60 種）。ただし振興局は北海道こそ
14 に分かれるが、**鹿児島と沖縄は 1 つのまま**で、離島の扱いが粗い。
そこで鹿児島・沖縄だけ気象庁の**予報区（office）**で細分する:

    鹿児島 → 奄美地方 / 鹿児島県（奄美地方除く）
    沖縄   → 沖縄本島 / 大東島 / 宮古島 / 八重山

予報区は class10s の境界 GeoJSON で座標判定する（offices の境界は
公開されていないが、class10 の親を辿れば office に届く）。

例外地点
--------
amedastable.json の ``type`` が例外を明示している。厳密にはアメダスの
地点網に入らないが、**アメダスとして配信されている**ものがある:

    A(56)  官署                      B(95)  官署
    C(1131) 通常のアメダス            D(1)   父島
    E(1)   南鳥島                    F(1)   富士山
    G(1)   秩父別

D/E/F/G は 1 地点ずつの特殊扱い。特に **F=富士山は標高 3,775m** で、
気圧が平地の 2/3（実測 652 hPa）になる。平地と同じ順位表や配色に混ぜると
誤読を招くので、下流が判別できるよう対応表に type を残す。

使い方:
    python build_area_map.py
"""
from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path

from weatherlib import jma

BASE = Path(__file__).resolve().parent
MASTER = BASE / "master"
URL_MASTER = "https://www.jma.go.jp/jma/kishou/know/amedas/ame_master.zip"
URL_TABLE = "https://www.jma.go.jp/bosai/amedas/const/amedastable.json"
URL_AREA = "https://www.jma.go.jp/bosai/common/const/area.json"
URL_GEO = "https://www.jma.go.jp/bosai/common/const/geojson/class10s.json"

# 振興局のままだと粗い府県。ここだけ予報区で細分する
SPLIT_PREFS = {"鹿児島", "沖縄"}
# 平地と同じ扱いにできない地点（下流での判別用。type は amedastable 由来）
SPECIAL_TYPES = {"D": "父島", "E": "南鳥島", "F": "富士山（標高3776m）", "G": "秩父別"}


def log(msg: str) -> None:
    print(f"[area-map] {msg}", flush=True)


def _in_ring(x: float, y: float, ring: list) -> bool:
    """点が多角形の中か（交差数判定）。外部依存を増やさないため自前で持つ。"""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _in_feature(x: float, y: float, geom: dict) -> bool:
    polys = (geom["coordinates"] if geom["type"] == "MultiPolygon"
             else [geom["coordinates"]])
    for poly in polys:
        if poly and _in_ring(x, y, poly[0]) and not any(
                _in_ring(x, y, hole) for hole in poly[1:]):
            return True
    return False


def load_master() -> dict[str, dict]:
    """ame_master を読む。同じ観測所番号が 2 行ある地点が 30 件ある
    （官署の「気象台の測器」と「アメダスの測器」）。**アメダス側を採る** —
    このサイトが扱うのはアメダスとしての観測値だから。備考1 に別番号が
    入っている行が気象台側なので、そちらを捨てる。"""
    raw = jma.http_get(URL_MASTER)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read(z.namelist()[0]).decode("cp932")
    out: dict[str, dict] = {}
    dropped = 0
    for r in csv.DictReader(io.StringIO(text)):
        code = r["観測所番号"]
        if code in out:
            # 2 行目。備考1 が「－」の側（＝アメダス）を残す
            if out[code]["備考1"] not in ("－", ""):
                out[code] = r
            else:
                dropped += 1
            continue
        out[code] = r
    log(f"ame_master: {len(out)} 地点（重複 {dropped} 行は気象台側として捨てた）")
    return out


def main() -> int:
    master = load_master()
    table = json.loads(jma.http_get(URL_TABLE))
    area = json.loads(jma.http_get(URL_AREA))
    offices, class10s = area["offices"], area["class10s"]

    # 鹿児島・沖縄の細分にだけ境界を使う（全国でやる必要はない）
    geo = json.loads(jma.http_get(URL_GEO))
    shapes = []
    for f in geo["features"]:
        parent = (class10s.get(f["properties"].get("code")) or {}).get("parent")
        if parent in offices and parent.startswith(("46", "47")):
            shapes.append((parent, f["geometry"]))
    log(f"鹿児島・沖縄の予報区境界: {len(shapes)}")

    stations: dict[str, dict] = {}
    split_hit = Counter()
    nudged = []
    for code, r in master.items():
        pref = r["都府県振興局"]
        lat = int(r["緯度(度)"]) + float(r["緯度(分)"]) / 60
        lon = int(r["経度(度)"]) + float(r["経度(分)"]) / 60
        area_code = pref
        if pref in SPLIT_PREFS:
            hit = next((o for o, g in shapes if _in_feature(lon, lat, g)), None)
            if hit is None:
                # 島は境界の外に落ちることがある。捨てず最近傍へ寄せる
                best, bd = None, None
                for o, g in shapes:
                    polys = (g["coordinates"] if g["type"] == "MultiPolygon"
                             else [g["coordinates"]])
                    for poly in polys:
                        for px, py in poly[0]:
                            d = (px - lon) ** 2 + (py - lat) ** 2
                            if bd is None or d < bd:
                                bd, best = d, o
                hit = best
                nudged.append((code, r["観測所名"], offices[hit]["name"], bd ** 0.5))
            area_code = offices[hit]["name"]
            split_hit[area_code] += 1
        t = (table.get(code) or {}).get("type")
        stations[code] = {
            "area": area_code, "pref": pref, "name": r["観測所名"],
            "kana": r["ｶﾀｶﾅ名"], "kind": r["種類"], "type": t,
            "lat": round(lat, 4), "lon": round(lon, 4),
            "alt": int(r["海面上の高さ(ｍ)"]) if r["海面上の高さ(ｍ)"].lstrip("-").isdigit() else None,
            "since": r["観測開始年月日"], "place": r["所在地"],
        }
        if t in SPECIAL_TYPES:
            # 平地と同じ順位表・配色に混ぜると誤読を招く地点
            stations[code]["special"] = SPECIAL_TYPES[t]

    per = Counter(v["area"] for v in stations.values())
    log(f"area_code {len(per)} 種 / 地点 {len(stations)}")
    log(f"  鹿児島・沖縄の細分: {dict(split_hit)}")
    if nudged:
        log(f"  境界外を最近傍へ寄せた {len(nudged)} 地点: "
            + ", ".join(f"{n}→{a}({d:.2f}度)" for _c, n, a, d in nudged[:4]))
    log(f"  1 area あたり 最大 {max(per.values())} / 最小 {min(per.values())} 地点")
    sp = {c: v["special"] for c, v in stations.items() if "special" in v}
    log(f"  例外地点 {len(sp)}: {sp}")

    out = MASTER / "area_map.json"
    out.write_text(json.dumps(
        {"note": "正は ame_master。振興局を基本に、鹿児島・沖縄のみ予報区で細分。"
                 "type が D/E/F/G の地点は平地と同じ扱いにしないこと",
         "source": URL_MASTER, "count": len(stations), "stations": stations},
        ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"master/area_map.json を出力（{out.stat().st_size/1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
