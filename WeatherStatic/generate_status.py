#!/usr/bin/env python3
"""「◯◯の状況」ページの生成（10 分ごと）。

気象庁は元データが 10 分値であるにもかかわらず、「気温の状況」「風の状況」を
**毎時 50 分頃**にしか更新していない。こちらは 10 分ごとに全 1,286 地点を
取っているので、そのまま 6 倍の頻度で出せる。ここが差別化になる。

サイト全体（1,840 ページ）を 10 分ごとに作り直すのは無駄なので、状況ページ
だけをこのスクリプトが受け持つ。generate.py には触らない。

入力:
    public_amedas/map/{ts}.json  … fetch_amedas_mirror.py が置いた 10 分値
    master/stations.json          … 地点マスタ（名前・都道府県・地方）
    master/normals/{code}.json    … 平年値（平年差の色分けに使う）

出力:
    public/Status/Temperature/index.html

使い方:
    python generate_status.py              # 全部
    python generate_status.py --only temp
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from weatherlib.filters import FILTERS
from weatherlib.svgchart import intraday_svg
# 平年値の読み方（daily は月キー・日は月内添字）は generate.py に正しい実装がある
from generate import normal_daily

BASE = Path(__file__).resolve().parent
MIRROR = BASE / "public_amedas" / "map"
MASTER = BASE / "master"
PUBLIC = BASE / "public"

JST = ZoneInfo("Asia/Tokyo")
SLOT_MINUTES = 10
# グラフに出す主要都市。値は map JSON のキー（アメダス番号）
GRAPH_CITIES = [("札幌", "14163"), ("東京", "44132"), ("大阪", "62078"), ("福岡", "82182")]
GRAPH_COLORS = ["#F92500", "#008000", "#0C00CC", "#B8860B"]
RANK_N = 20

# 風向コード。0=静穏、1=北北東 … 16=北（時計回り）。回転を間違えると
# 全地点の風向が狂うので、根室・那覇の卓越風で妥当性を確認済み。
WDIR = ["静穏", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東", "南",
        "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西", "北"]
# 矢印は「風が吹いていく向き」ではなく「風が吹いてくる向き」を指す慣例に合わせ、
# 北風（16）なら下向き矢印にする（風向は風が吹いてくる方角）
WDIR_DEG = {i: (i * 22.5) % 360 for i in range(1, 17)}

REGIONS = [("hokkaido", "北海道", 11, 24), ("tohoku", "東北", 31, 36),
           ("kanto", "関東", 40, 46), ("koshin", "甲信", 48, 49),
           ("tokai", "東海", 50, 53), ("hokuriku", "北陸", 54, 58),
           ("kinki", "近畿", 60, 65), ("chugoku", "中国", 66, 69),
           ("shikoku", "四国", 71, 74), ("kyushu", "九州・沖縄", 81, 94)]


def log(msg: str) -> None:
    print(f"[status] {msg}", flush=True)


def slot_date(name: str) -> date:
    return date(int(name[:4]), int(name[4:6]), int(name[6:8]))


def slot_minutes(name: str) -> int:
    return int(name[8:10]) * 60 + int(name[10:12])


def load_slots(day: date) -> list[str]:
    """その日の 10 分値スロット名を古い順に返す。"""
    return sorted(p.stem for p in MIRROR.glob(f"{day:%Y%m%d}*.json"))


def region_of(prec_no: int | None) -> str:
    for key, _name, lo, hi in REGIONS:
        if prec_no is not None and lo <= prec_no <= hi:
            return key
    return ""


def normal_tmax_tmin(code: int, d: date) -> tuple[int | None, int | None]:
    """その日の最高・最低の平年値。daily は月ごとのキーで日は月内の添字。"""
    return normal_daily(code, "tmax", d), normal_daily(code, "tmin", d)


def build_temperature(env: Environment, stations: dict) -> None:
    now = datetime.now(JST).replace(tzinfo=None)
    slots = load_slots(now.date()) or load_slots(now.date() - timedelta(days=1))
    if not slots:
        log("10 分値が無いため気温の状況は生成しない（fetch_amedas_mirror.py が未実行?）")
        return
    latest = slots[-1]
    snap = json.loads((MIRROR / f"{latest}.json").read_bytes())
    obs_time = datetime(*map(int, (latest[:4], latest[4:6], latest[6:8],
                                   latest[8:10], latest[10:12])))

    st = stations["stations"]
    a2c = stations["index"]["amedas_to_code"]
    rows = []
    for amedas, entry in snap.items():
        e = entry.get("temp")
        if not (isinstance(e, list) and len(e) >= 2 and e[0] is not None and e[1] == 0):
            continue
        code = a2c.get(amedas)
        rec = st.get(str(code)) if code else None
        if rec is None:
            continue
        t = int(round(float(e[0]) * 10))
        n_max, n_min = normal_tmax_tmin(int(code), now.date())
        # 平年差は「その時刻の平年値」が無いので、最高と最低の中点を基準にする
        mid = (n_max + n_min) // 2 if n_max is not None and n_min is not None else None
        rows.append({
            "code": int(code), "amedas": amedas, "name": rec["name"],
            "pref": rec.get("pref") or "", "region": region_of(
                (rec.get("etrn") or {}).get("prec_no")),
            "temp": t, "normal_mid": mid,
            "diff": (t - mid) if mid is not None else None,
            "place": rec.get("place") or "",
        })
    rows.sort(key=lambda r: r["temp"], reverse=True)

    # 当日の 10 分値推移（主要都市）。気象庁の同名ページには無い粒度
    series, minutes = [], [slot_minutes(s) for s in slots]
    cache = {s: json.loads((MIRROR / f"{s}.json").read_bytes()) for s in slots}
    for (label, key), color in zip(GRAPH_CITIES, GRAPH_COLORS):
        vals = []
        for s in slots:
            e = cache[s].get(key, {}).get("temp")
            vals.append(int(round(float(e[0]) * 10))
                        if isinstance(e, list) and e[0] is not None and e[1] == 0 else None)
        if any(v is not None for v in vals):
            series.append({"label": label, "color": color, "values": vals})

    chart = intraday_svg(f"{obs_time:%m月%d日}の気温推移（10 分値）",
                         minutes, series, width=680, height=280)

    html = env.get_template("status/temperature.html").render(
        page_title="気温の状況（10 分ごと更新）", nav_active="status",
        build_year=now.year, obs_time=obs_time, n_stations=len(rows),
        highs=rows[:RANK_N], lows=list(reversed(rows[-RANK_N:])), rows=rows,
        region_filters=[(k, n) for k, n, _, _ in REGIONS],
        chart=chart, graph_cities=[c[0] for c in GRAPH_CITIES],
        graph_colors=GRAPH_COLORS, slot_count=len(slots))
    out = PUBLIC / "Status" / "Temperature" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log(f"Status/Temperature/index.html ({len(html):,} bytes) "
        f"/ {len(rows)} 地点 / {obs_time:%H:%M} 現在 / グラフ {len(slots)} 点")


def build_wind(env: Environment, stations: dict) -> None:
    """風の状況。気象庁の同名ページは毎時 50 分頃の更新なので、10 分値がそのまま差になる。

    map JSON にあるのは 10 分値の風速・風向で、日最大瞬間風速は入っていない。
    そこで「今の風」と「今日ここまでの最大（10 分値ベース）」を出す。
    瞬間値ではないことはページに明記する。
    """
    now = datetime.now(JST).replace(tzinfo=None)
    slots = load_slots(now.date()) or load_slots(now.date() - timedelta(days=1))
    if not slots:
        log("10 分値が無いため風の状況は生成しない")
        return
    latest = slots[-1]
    obs_time = datetime(*map(int, (latest[:4], latest[4:6], latest[6:8],
                                   latest[8:10], latest[10:12])))
    st = stations["stations"]
    a2c = stations["index"]["amedas_to_code"]

    # 当日の最大風速はここまでの全スロットから拾う
    peak: dict[str, tuple[int, int, str]] = {}     # amedas -> (風速x10, 風向, 時刻)
    for s in slots:
        snap = json.loads((MIRROR / f"{s}.json").read_bytes())
        for amedas, entry in snap.items():
            e = entry.get("wind")
            if not (isinstance(e, list) and len(e) >= 2
                    and e[0] is not None and e[1] == 0):
                continue
            v = int(round(float(e[0]) * 10))
            if amedas not in peak or v > peak[amedas][0]:
                d = entry.get("windDirection")
                dv = d[0] if isinstance(d, list) and d[0] is not None else 0
                peak[amedas] = (v, dv, f"{s[8:10]}:{s[10:12]}")

    snap = json.loads((MIRROR / f"{latest}.json").read_bytes())
    rows = []
    for amedas, entry in snap.items():
        e = entry.get("wind")
        if not (isinstance(e, list) and len(e) >= 2
                and e[0] is not None and e[1] == 0):
            continue
        code = a2c.get(amedas)
        rec = st.get(str(code)) if code else None
        if rec is None:
            continue
        d = entry.get("windDirection")
        dv = d[0] if isinstance(d, list) and d[0] is not None else 0
        pk = peak.get(amedas)
        rows.append({
            "name": rec["name"], "pref": rec.get("pref") or "",
            "region": region_of((rec.get("etrn") or {}).get("prec_no")),
            "wind": int(round(float(e[0]) * 10)),
            "dir": dv, "dir_name": WDIR[dv], "deg": WDIR_DEG.get(dv),
            "peak": pk[0] if pk else None,
            "peak_dir": WDIR[pk[1]] if pk else "",
            "peak_at": pk[2] if pk else "",
        })
    rows.sort(key=lambda r: r["wind"], reverse=True)
    by_peak = sorted((r for r in rows if r["peak"] is not None),
                     key=lambda r: r["peak"], reverse=True)

    # 風向の分布（今の風がどちらから吹いているか）
    dist = [0] * 17
    for r in rows:
        dist[r["dir"]] += 1

    html = env.get_template("status/wind.html").render(
        page_title="風の状況（10 分ごと更新）", nav_active="status",
        build_year=now.year, obs_time=obs_time, n_stations=len(rows),
        now_top=rows[:RANK_N], peak_top=by_peak[:RANK_N], rows=rows,
        region_filters=[(k, n) for k, n, _, _ in REGIONS],
        dist=[(WDIR[i], dist[i]) for i in range(1, 17)],
        calm=dist[0], slot_count=len(slots),
        dist_max=max(dist[1:]) or 1)
    out = PUBLIC / "Status" / "Wind" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log(f"Status/Wind/index.html ({len(html):,} bytes) / {len(rows)} 地点 / "
        f"{obs_time:%H:%M} 現在 / 最大 {by_peak[0]['peak']/10 if by_peak else 0} m/s")


def build_records(env: Environment, stations: dict) -> None:
    """観測史上1位・各月1位の更新状況。

    記録値は data/today.csv に入っている**気象庁 mdrr CSV の公式値**を使う
    （自前で observations.nc から求めるより正確で、統計期間の扱いも気象庁に従える）。
    10 分値ベースの記録は扱わない。ここで比べるのは日別統計だけ。

    今日の値が記録と等しい場合を「タイ」として拾う。mdrr の記録欄が当日値を
    含むかは気象庁側の都合で変わりうるので、超過（>）だけを見ると取りこぼす。
    """
    src = BASE / "data" / "today.csv"
    if not src.exists():
        log("data/today.csv が無いため記録更新は生成しない（fetch_data.py が未実行?）")
        return
    import csv

    st = stations["stations"]
    now = datetime.now(JST).replace(tzinfo=None)

    def num(s: str) -> int | None:
        try:
            v = int(s)
        except (TypeError, ValueError):
            return None
        return None if v <= -999 else v

    hot_all, hot_mon, cold_all, cold_mon, near = [], [], [], [], []
    n_rows = 0
    with src.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            n_rows += 1
            rec = st.get(r["code"])
            if rec is None:
                continue
            base = {"name": rec["name"], "pref": rec.get("pref") or "",
                    "region": region_of((rec.get("etrn") or {}).get("prec_no"))}
            tmax, tmin = num(r["tmax"]), num(r["tmin"])

            for val, key, dkey, bucket, higher in (
                    (tmax, "record_tmax", "record_tmax_date", hot_all, True),
                    (tmax, "month_tmax", "month_tmax_date", hot_mon, True),
                    (tmin, "record_tmin", "record_tmin_date", cold_all, False),
                    (tmin, "month_tmin", "month_tmin_date", cold_mon, False)):
                old = num(r[key])
                if val is None or old is None:
                    continue
                hit = val >= old if higher else val <= old
                if hit:
                    bucket.append({**base, "value": val, "old": old,
                                   "old_date": r[dkey],
                                   "tie": val == old,
                                   "at": r["tmax_at" if higher else "tmin_at"],
                                   "q": r["tmax_q" if higher else "tmin_q"]})

            # 記録に迫った地点（1.0℃ 以内）。更新がゼロの日でもページが死なないように
            old = num(r["record_tmax"])
            if tmax is not None and old is not None and 0 < old - tmax <= 10:
                near.append({**base, "value": tmax, "old": old,
                             "old_date": r["record_tmax_date"], "gap": old - tmax})

    for b in (hot_all, hot_mon, cold_all, cold_mon):
        b.sort(key=lambda x: x["value"] - x["old"], reverse=True)
    near.sort(key=lambda x: x["gap"])

    html = env.get_template("status/records.html").render(
        page_title="観測史上1位の更新状況", nav_active="status",
        build_year=now.year, now=now, n_stations=n_rows,
        hot_all=hot_all, hot_mon=hot_mon[:RANK_N],
        cold_all=cold_all, cold_mon=cold_mon[:RANK_N],
        near=near[:RANK_N],
        n_hot_mon=len(hot_mon), n_cold_mon=len(cold_mon))
    out = PUBLIC / "Status" / "Records" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log(f"Status/Records/index.html ({len(html):,} bytes) / {n_rows} 地点 / "
        f"史上1位 高 {len(hot_all)}・低 {len(cold_all)} / "
        f"月1位 高 {len(hot_mon)}・低 {len(cold_mon)}")


def build_lab(env: Environment) -> None:
    """ブラウザ内 Python でグラフを描くページ。

    サーバー側と **同じ svgchart.py** を Pyodide に読ませる。外部依存が無い
    モジュールなので numpy を落とさずに済み（Python 本体だけで約 5MB）、
    「サイトの図を作っている Python が、そのままブラウザで動く」という
    見せ方ができる。Pyodide の起動はボタンを押したときだけ（この規模を
    全訪問者に配るのは筋が悪いし、取得はユーザー操作時のみという方針にも合う）。
    """
    src = (BASE / "weatherlib" / "svgchart.py").read_text(encoding="utf-8")
    html = env.get_template("status/lab.html").render(
        page_title="Python グラフ工房（ブラウザ内 Python）",
        nav_active="status", build_year=datetime.now(JST).year,
        svgchart_src=src, pyodide_version="0.28.0")
    out = PUBLIC / "Status" / "Lab" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log(f"Status/Lab/index.html ({len(html):,} bytes / svgchart.py {len(src):,} 文字を同梱)")


# 工房で選べる要素。(map JSON のキー, 表示名, 倍率, 単位, グラフ種別)
# 単位も値域も要素ごとに違うので、倍率と種別をここで一元管理する。
# 積算量（降水・日照）を折れ線にすると誤読するため棒グラフにする。
LAB_ELEMENTS = [
    ("temp",             "気温",       10, "℃",   "line"),
    ("humidity",         "湿度",        1, "%",    "line"),
    ("pressure",         "現地気圧",    10, "hPa",  "line"),
    ("normalPressure",   "海面気圧",    10, "hPa",  "line"),
    ("wind",             "風速",       10, "m/s",  "line"),
    ("precipitation10m", "10分降水量", 10, "mm",   "bar"),
    ("precipitation1h",  "1時間降水量", 10, "mm",   "bar"),
    ("sun10m",           "10分日照",    1, "分",   "bar"),
    ("snow",             "積雪深",      1, "cm",   "line"),
]


def build_today_json(stations: dict) -> None:
    """当日の 10 分値を**要素ごとに**ファイル分けして出す（ブラウザのグラフ用）。

    生の map JSON を 144 本取らせると 35MB になる。かといって全要素を 1 本に
    まとめると数 MB になり、初回表示が重い。要素ごとに分ければブラウザは
    選ばれた 1 要素だけを取ればよい（1 ファイル gzip 後およそ 60KB）。
    """
    now = datetime.now(JST).replace(tzinfo=None)
    slots = load_slots(now.date()) or load_slots(now.date() - timedelta(days=1))
    if not slots:
        return
    day = slot_date(slots[-1])
    st = stations["stations"]
    a2c = stations["index"]["amedas_to_code"]
    minutes = [slot_minutes(s) for s in slots]

    # 1 周だけ読んで全要素を同時に振り分ける（144 回 × 要素数の再読込を避ける）
    acc: dict[str, dict[str, dict]] = {k: {} for k, _, _, _, _ in LAB_ELEMENTS}
    for j, s in enumerate(slots):
        snap = json.loads((MIRROR / f"{s}.json").read_bytes())
        for amedas, entry in snap.items():
            m = None
            for key, _label, mul, _unit, _kind in LAB_ELEMENTS:
                e = entry.get(key)
                if not (isinstance(e, list) and len(e) >= 2
                        and e[0] is not None and e[1] == 0):
                    continue
                rec = acc[key].get(amedas)
                if rec is None:
                    if m is None:
                        code = a2c.get(amedas)
                        m = st.get(str(code)) if code else False
                    if not m:
                        continue
                    rec = acc[key][amedas] = {
                        "name": m["name"], "pref": m.get("pref") or "",
                        "v": [None] * len(slots)}
                rec["v"][j] = int(round(float(e[0]) * mul))

    index = []
    out_dir = PUBLIC / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    for key, label, mul, unit, kind in LAB_ELEMENTS:
        data = acc[key]
        if not data:
            continue                      # 夏の積雪など、その日に観測が無い要素は出さない
        body = {"date": day.isoformat(), "element": key, "label": label,
                "scale": mul, "unit": unit, "kind": kind, "minutes": minutes,
                "attribution": "出典: 気象庁ホームページ（編集・加工: AIseed）",
                "stations": data}
        p = out_dir / f"amedas-today-{key}.json"
        p.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                     encoding="utf-8")
        index.append({"element": key, "label": label, "unit": unit, "kind": kind,
                      "stations": len(data), "bytes": p.stat().st_size})
    (out_dir / "amedas-today.json").write_text(
        json.dumps({"date": day.isoformat(), "slots": len(slots),
                    "elements": index}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    log("data/amedas-today-*.json: "
        + " / ".join(f"{i['label']} {i['stations']}地点 {i['bytes']//1024}KB"
                     for i in index))


def main() -> int:
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    stations = json.loads((MASTER / "stations.json").read_text(encoding="utf-8"))
    env = Environment(loader=FileSystemLoader(BASE / "templates"),
                      autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters.update(FILTERS)
    env.globals["css_version"] = "status"
    env.globals["legacy_layout"] = False

    if only in (None, "temp"):
        build_temperature(env, stations)
    if only in (None, "wind"):
        build_wind(env, stations)
    if only in (None, "records"):
        build_records(env, stations)
    if only in (None, "lab"):
        build_today_json(stations)
        build_lab(env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
