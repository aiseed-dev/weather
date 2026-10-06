"""実況のページの地図（地点を色で塗った SVG）。ページを作るときに書き込む。

輪郭はトップの地図と同じ assets/japan_outline.json（make_japan_outline.py。Natural Earth 10m）。
本土を大きく描き、沖縄・奄美は右下の差し込み図。どちらの枠にも入らない地点（大東島・小笠原・
南鳥島など）は描かず、地図の下に名前を出す。

配色はデスクトップアプリのアメダスの地図（src/aiseed_weather/figures/amedas_map.py）と同じ:
気温と風速はモデルの図の色（T2M・WIND10M）、降水量は気象庁の配色、積雪深はアプリの積雪の色。
"""
from __future__ import annotations

import json
import math
from html import escape
from pathlib import Path

from weatherlib.filters import TEMP_ANCHORS

OUTLINE = json.loads((Path(__file__).resolve().parent.parent / "assets" / "japan_outline.json")
                     .read_text(encoding="utf-8"))

# 値は物理量（℃・m/s・mm・cm）
TEMP = [(t / 10, c) for t, c in TEMP_ANCHORS]
WIND = [(0.0, (220, 230, 235)), (5.0, (140, 200, 215)), (10.0, (90, 180, 130)),
        (15.0, (215, 210, 90)), (20.0, (235, 145, 55)), (25.0, (210, 60, 60)),
        (40.0, (110, 30, 110))]                                  # アプリの WIND10M
PRECIP_BOUNDS = (0.0, 1.0, 5.0, 10.0, 20.0, 30.0, 50.0, 80.0)  # 気象庁の降水量の配色
PRECIP_COLOURS = ("#f2f2ff", "#a0d2ff", "#218cff", "#0041ff",
                  "#faf500", "#ff9900", "#ff2800", "#b40068")
SNOW = [(0.0, (225, 235, 245)), (50.0, (120, 170, 220)),
        (150.0, (60, 80, 180)), (300.0, (110, 40, 140))]          # アプリの積雪深


def ramp(anchors: list, v: float) -> str:
    """連続の配色。端の外は端の色。"""
    if v <= anchors[0][0]:
        c = anchors[0][1]
    elif v >= anchors[-1][0]:
        c = anchors[-1][1]
    else:
        for (v0, c0), (v1, c1) in zip(anchors, anchors[1:]):
            if v0 <= v <= v1:
                f = (v - v0) / (v1 - v0)
                c = tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))
                break
    return "#{:02x}{:02x}{:02x}".format(*c)


def precip_colour(v: float) -> str:
    """気象庁の降水量の階級（0〜1 mm 未満、1〜5 … 80 mm 以上）。"""
    i = max(k for k, b in enumerate(PRECIP_BOUNDS) if v >= b)
    return PRECIP_COLOURS[i]


def ramp_legend(anchors: list, ticks: list, unit: str) -> dict:
    """連続の凡例（CSS のグラデーションと目盛り）。"""
    lo, hi = anchors[0][0], anchors[-1][0]
    stops = ", ".join(f"{ramp(anchors, a)} {100 * (a - lo) / (hi - lo):.1f}%" for a, _ in anchors)
    return {"kind": "ramp", "gradient": f"linear-gradient(90deg, {stops})",
            "ticks": [(100 * (t - lo) / (hi - lo), f"{t:g}") for t in ticks], "unit": unit}


def precip_legend() -> dict:
    labels = ["1 未満", "1〜", "5〜", "10〜", "20〜", "30〜", "50〜", "80〜"]
    return {"kind": "bins", "bins": list(zip(PRECIP_COLOURS, labels)), "unit": "mm"}


def project(lat: float, lon: float) -> tuple[float, float] | None:
    """地図の座標。どちらの枠にも入らなければ None。"""
    m, i, kx = OUTLINE["main"], OUTLINE["inset"], OUTLINE["kx"]
    if lat < i["lat_max"] and lon < i["lon_max"]:
        x = i["x"] + (lon - i["lon0"]) * kx * i["scale"]
        y = i["y"] + (i["lat1"] - lat) * i["scale"]
        if i["x"] <= x <= i["x"] + i["w"] and i["y"] <= y <= i["y"] + i["h"]:
            return x, y
        return None
    w, h = (float(v) for v in OUTLINE["viewBox"].split()[2:])
    x = (lon - m["lon0"]) * kx * m["scale"]
    y = (m["lat1"] - lat) * m["scale"]
    if 0 <= x <= w and 0 <= y <= h and not (i["x"] <= x <= i["x"] + i["w"] and i["y"] <= y <= i["y"] + i["h"]):
        return x, y
    return None


def map_svg(points: list[dict], label: str) -> dict:
    """地点を色で塗った地図。

    points は {lat, lon, colour, title, url, (amedas), (small), (deg, speed)} の並び。後ろほど上に描く
    （強い所・多い所を上にしたいなら、弱い順に並べて渡す）。small は値の無い地点・0 の地点を
    小さく灰色で描く。deg（風が吹いてくる方位、北 0・時計回り）があれば、風下へ向かう線を描く。
    戻り値は {"svg": 文字列, "outside": [地図の外の地点の名前]}。
    """
    i = OUTLINE["inset"]
    parts = [f'<svg class="smap-svg" viewBox="{OUTLINE["viewBox"]}" role="img" aria-label="{escape(label)}">',
             f'<defs><clipPath id="smap-inset"><rect x="{i["x"]}" y="{i["y"]}" width="{i["w"]}" height="{i["h"]}"/></clipPath></defs>',
             f'<path class="smap-land" d="{OUTLINE["main"]["d"]}"/>',
             f'<rect class="smap-inset" x="{i["x"]}" y="{i["y"]}" width="{i["w"]}" height="{i["h"]}" rx="3"/>',
             f'<path class="smap-land" clip-path="url(#smap-inset)" d="{i["d"]}"/>']
    outside = []
    for p in points:
        xy = project(p["lat"], p["lon"])
        if xy is None:
            outside.append(p["name"])
            continue
        x, y = xy
        if p.get("small"):
            dot = f'<circle class="smap-zero" cx="{x:.1f}" cy="{y:.1f}" r="0.9">'
        else:
            dot = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.9" fill="{p["colour"]}">'
        arrow = ""
        if p.get("deg") is not None and p.get("speed", 0) >= 1:
            a = math.radians(p["deg"] + 180)               # 風下
            n = 2.4 + min(p["speed"], 20) / 20 * 3.2
            arrow = (f'<line class="smap-wind" x1="{x:.1f}" y1="{y:.1f}" '
                     f'x2="{x + math.sin(a) * n:.1f}" y2="{y - math.cos(a) * n:.1f}"/>')
        title = f'<title>{escape(p["title"])}</title>'
        body = arrow + dot + title + "</circle>"
        mark = f' data-a="{escape(str(p["amedas"]))}"' if p.get("amedas") else ""   # 自分の地点の目印
        parts.append(f'<a href="{escape(p["url"])}"{mark}>{body}</a>' if p.get("url") else body)
    parts.append("</svg>")
    return {"svg": "".join(parts), "outside": outside}
