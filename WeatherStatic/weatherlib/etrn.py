"""etrn（気象庁「過去の気象データ検索」）の地点一覧の取得・解析。

地点選択ページ（prefecture.php?prec_no=NN）の viewPoint(...) JS から、
その府県の全地点（現役＋廃止）の地点番号・名前・座標・観測要素・観測終了日を取り出す。

地点番号（block_no）は本プロジェクトの主キー code の供給源:
  官署        … 5 桁（国際地点番号と同一。type='s'）
  アメダス単独 … 4 桁の独自番号（type='a'）
"""
from __future__ import annotations

import re
import time
from typing import Iterator

from weatherlib import jma

URL_PREFS = "https://www.data.jma.go.jp/stats/etrn/select/prefecture00.php"
URL_PREF = ("https://www.data.jma.go.jp/stats/etrn/select/prefecture.php"
            "?prec_no={prec}&block_no=&year=&month=&day=&view=")
INTERVAL = 1.0

# viewPoint(as, bk_no, ch, ch_kn, lat_d, lat_m, lon_d, lon_m, height,
#           f_pre, f_wsp, f_tem, f_sun, f_snc, f_hum, ed_y, ed_m, ed_d, bikou1..5)
_VIEWPOINT = re.compile(
    r"viewPoint\('([as])','(\d+)','([^']*)','([^']*)',"      # 種別, 地点番号, 名前, カナ
    r"'(\d+)','([\d.]+)','(\d+)','([\d.]+)','([^']*)',"      # 緯度(度,分), 経度(度,分), 標高
    r"'(\d+)','(\d+)','(\d+)','(\d+)','(\d+)','(\d+)',"      # 観測要素フラグ×6
    r"'(\d+)','(\d+)','(\d+)'"                                # 観測終了 年,月,日 (9999=現役)
)

FLAG_NAMES = ("precip", "wind", "temp", "sun", "snow", "humidity")


def fetch_prec_list() -> list[int]:
    """府県番号（prec_no）の一覧。"""
    html = jma.http_get(URL_PREFS).decode("utf-8", errors="replace")
    return sorted({int(m) for m in re.findall(r"prec_no=(\d+)", html)})


def parse_pref_page(html: str, prec: int) -> Iterator[dict]:
    """1 府県ページの viewPoint エントリをすべて返す（重複は除去）。"""
    seen = set()
    for m in _VIEWPOINT.finditer(html):
        (typ, block, name, kana, lat_d, lat_m, lon_d, lon_m, height,
         f_pre, f_wsp, f_tem, f_sun, f_snc, f_hum, ed_y, ed_m, ed_d) = m.groups()
        key = (typ, block, name, ed_y, ed_m, ed_d)
        if key in seen:
            continue
        seen.add(key)
        active = ed_y == "9999"
        try:
            alt = float(height)
        except ValueError:
            alt = None
        yield {
            "prec_no": prec,
            "block_no": block,
            "type": typ,                          # 's'=官署, 'a'=アメダス
            "name": name,
            "kana": kana,
            "lat": round(int(lat_d) + float(lat_m) / 60, 4),
            "lon": round(int(lon_d) + float(lon_m) / 60, 4),
            "lat_dm": [int(lat_d), float(lat_m)],  # 度・分（突合用の原表現）
            "lon_dm": [int(lon_d), float(lon_m)],
            "alt": alt,
            "flags": dict(zip(FLAG_NAMES,
                              map(int, (f_pre, f_wsp, f_tem, f_sun, f_snc, f_hum)))),
            "active": active,
            "end": None if active else f"{ed_y}-{ed_m.zfill(2)}-{ed_d.zfill(2)}",
        }


def fetch_all(log=print, interval: float = INTERVAL) -> list[dict]:
    """全府県の地点一覧（現役＋廃止）を取得する。"""
    precs = fetch_prec_list()
    log(f"etrn 地点一覧を取得中...（{len(precs)} 府県、約 {int(len(precs) * interval)} 秒）")
    entries: list[dict] = []
    for prec in precs:
        html = jma.http_get(URL_PREF.format(prec=prec)).decode("utf-8", errors="replace")
        entries.extend(parse_pref_page(html, prec))
        time.sleep(interval)
    entries.sort(key=lambda e: (e["prec_no"], e["type"], e["block_no"]))
    return entries
