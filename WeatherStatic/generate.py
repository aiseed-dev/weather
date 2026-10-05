#!/usr/bin/env python3
"""WeatherCore 静的サイトジェネレータ（描画層）。

data/（現在値スナップショット）＋ master/（地点・平年値）＋
store/observations.nc（履歴。読み取り専用）から public/*.html を生成する。
ネットワークアクセスは行わない（DATA_CONTRACT v2.1）。

使い方:
    python generate.py [--clean]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from weatherlib.filters import FILTERS, bcolor
from weatherlib.season import is_season, is_summer, winter_start
from weatherlib.stations import MAIN_STATIONS

BASE = Path(__file__).resolve().parent
TEMPLATES = BASE / "templates"
DATA = BASE / "data"
MASTER = BASE / "master"
STORE_NC = BASE / "store" / "observations.nc"
PUBLIC = BASE / "public"

# 静的アセットの取得元。旧サイト WeatherCore の wwwroot から取り込んだ
# WeatherStatic/wwwroot/ を既定にする（2026-07-07 に取り込み済み。旧リポジトリは
# 削除してよい）。環境変数 WEATHERCORE_WWWROOT で上書き可能。
import os as _os
_candidates = [
    Path(_os.environ["WEATHERCORE_WWWROOT"]) if "WEATHERCORE_WWWROOT" in _os.environ else None,
    BASE / "wwwroot",
    BASE.parent / "WeatherCore" / "WeatherCore" / "wwwroot",
    BASE.parent.parent / "WeatherCore" / "WeatherCore" / "wwwroot",
]
WWWROOT = next((p for p in _candidates if p and p.is_dir()), BASE / "wwwroot")
# css/ と javascripts/ は Bootstrap 3・jQuery 用だったので配信しない。
# スタイルは assets/site-base.css + assets/site.css の 2 枚だけで足りる。
ASSET_PATHS = ["Images", "favicon.ico", "robots.txt"]


# ---------------------------------------------------------------- 入力の読み込み

def load_today() -> tuple[dict[int, dict], dict]:
    """data/today.csv + today_meta.json → ({code: 行}, メタ)。数値列は int 化。"""
    rows: dict[int, dict] = {}
    with (DATA / "today.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rec = dict(r)
            for k, val in rec.items():
                if k in ("amedas", ) or k.endswith("_at") or k.endswith("_date"):
                    continue
                rec[k] = int(val) if val != "" else None
            rows[rec["code"]] = rec
    meta = json.loads((DATA / "today_meta.json").read_text(encoding="utf-8"))
    return rows, meta


def load_forecast() -> dict:
    return json.loads((DATA / "forecast.json").read_text(encoding="utf-8"))


def load_stations() -> dict:
    return json.loads((MASTER / "stations.json").read_text(encoding="utf-8"))


def load_normals(code: int) -> dict | None:
    p = MASTER / "normals" / f"{code}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


_SLUG_BY_AMEDAS: dict[str, str] = {}


def station_slug(rec: dict) -> str:
    """Stations/JP・Climate/Chart 共通の URL slug。

    climate_targets() が衝突解決済みの slug を _SLUG_BY_AMEDAS に登録する
    （main() の先頭で必ず計算される）。未登録地点はリンク先ページ自体が
    無いので、素の小文字化で返す（リンクは生成側で張らないこと）。
    """
    s = _SLUG_BY_AMEDAS.get(str(rec.get("amedas")))
    if s:
        return s
    return (rec.get("place") or rec.get("en") or "").lower().replace(" ", "-")


def normal_daily(code: int, elem: str, d: date) -> int | None:
    nml = load_normals(code)
    if nml is None:
        return None
    try:
        return nml["daily"][str(d.month)][elem][d.day - 1]
    except (KeyError, IndexError):
        return None


# ---------------------------------------------------------------- 履歴（observations.nc）

class History:
    """observations.nc の読み取り専用ラッパ（日数集計・期間内の極値）。"""

    def __init__(self):
        self.ds = None
        if STORE_NC.exists():
            import netCDF4 as nc
            self.ds = nc.Dataset(STORE_NC)
            self.ds.set_auto_mask(False)

    def _slice(self, var: str, row: int, start: date, end: date):
        from weatherlib.ncstore import date_index
        if self.ds is None:
            return None
        j0, j1 = date_index(start), date_index(end) + 1
        n_date = self.ds.dimensions["date"].size
        if j0 >= n_date:
            return None
        return self.ds[var][row, j0:min(j1, n_date)]

    def day_counts(self, row: int, start: date, end: date) -> dict[str, int]:
        """期間内の日数（猛暑日・真夏日・夏日・真冬日・熱帯夜・冬日）。"""
        from weatherlib.ncstore import FILL
        out = {"moushobi": 0, "manatsubi": 0, "natsubi": 0, "mafuyubi": 0,
               "nettaiya": 0, "fuyubi": 0}
        tmax = self._slice("tmax", row, start, end)
        if tmax is not None:
            ok = tmax != FILL
            out.update(
                moushobi=int(((tmax >= 350) & ok).sum()),
                manatsubi=int(((tmax >= 300) & ok).sum()),
                natsubi=int(((tmax >= 250) & ok).sum()),
                mafuyubi=int(((tmax < 0) & ok).sum()),
            )
        tmin = self._slice("tmin", row, start, end)
        if tmin is not None:
            ok = tmin != FILL
            out.update(
                nettaiya=int(((tmin >= 250) & ok).sum()),
                fuyubi=int(((tmin < 0) & ok).sum()),
            )
        return out

    def extreme(self, var: str, row: int, start: date, end: date,
                highest: bool) -> tuple[int | None, date | None]:
        """期間内の極値（highest=True で最大）とその起日。"""
        from weatherlib.ncstore import FILL
        arr = self._slice(var, row, start, end)
        if arr is None:
            return None, None
        ok = arr != FILL
        if not ok.any():
            return None, None
        import numpy as np
        if highest:
            idx = int(np.where(ok, arr, -32768).argmax())
        else:
            idx = int(np.where(ok, arr, 32767).argmin())
        return int(arr[idx]), start + timedelta(days=idx)

    def matrix(self, var: str, start: date, end: date):
        """全地点×期間の行列（numpy）。全地点ページの日数集計・極値用。"""
        from weatherlib.ncstore import date_index
        if self.ds is None:
            return None
        j0, j1 = date_index(start), date_index(end) + 1
        n_date = self.ds.dimensions["date"].size
        if j0 >= n_date:
            return None
        return self.ds[var][:, j0:min(j1, n_date)]

    def all_day_counts(self, start: date, end: date) -> dict[str, "object"]:
        """全地点の日数集計（行番号 index の配列で返す）。"""
        import numpy as np
        from weatherlib.ncstore import FILL
        out = {}
        tmax = self.matrix("tmax", start, end)
        tmin = self.matrix("tmin", start, end)
        z = np.zeros(0, dtype=int)
        if tmax is not None:
            ok = tmax != FILL
            out.update(moushobi=((tmax >= 350) & ok).sum(axis=1),
                       manatsubi=((tmax >= 300) & ok).sum(axis=1),
                       natsubi=((tmax >= 250) & ok).sum(axis=1),
                       mafuyubi=((tmax < 0) & ok).sum(axis=1))
        if tmin is not None:
            ok = tmin != FILL
            out.update(nettaiya=((tmin >= 250) & ok).sum(axis=1),
                       fuyubi=((tmin < 0) & ok).sum(axis=1))
        return out

    def all_extremes(self, var: str, start: date, end: date, highest: bool):
        """全地点の期間極値と起日 index。戻り値 (値配列, 起日offset配列, 有効mask)。"""
        import numpy as np
        from weatherlib.ncstore import FILL
        m = self.matrix(var, start, end)
        if m is None:
            return None
        ok = m != FILL
        has = ok.any(axis=1)
        if highest:
            idx = np.where(ok, m, -32768).argmax(axis=1)
        else:
            idx = np.where(ok, m, 32767).argmin(axis=1)
        vals = m[np.arange(m.shape[0]), idx]
        return vals, idx, has

    def series(self, var: str, row: int, start: date, end: date) -> list[int | None]:
        """期間内の日別値リスト（欠測は None）。Home のグラフ用。"""
        from weatherlib.ncstore import FILL
        arr = self._slice(var, row, start, end)
        n_days = (end - start).days + 1
        if arr is None:
            return [None] * n_days
        vals = [int(x) if x != FILL else None for x in arr]
        return vals + [None] * (n_days - len(vals))

    def close(self):
        if self.ds is not None:
            self.ds.close()


# ---------------------------------------------------------------- 共通

def make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(FILTERS)
    # CSS のキャッシュバスティング（内容ハッシュを ?v= に付ける）。
    # モダン版は site-base.css も読むので、両方を混ぜたハッシュにする
    import hashlib, os
    h = hashlib.md5()
    for name in ("site.css", "site-base.css"):
        p = BASE / "assets" / name
        if p.exists():
            h.update(p.read_bytes())
    env.globals["css_version"] = h.hexdigest()[:10]
    return env


def copy_assets() -> None:
    # 黙って飛ばすと全ページのロゴと天気アイコンが壊れたまま公開されるので、
    # 見つからなければ止める（2026-08-28、当時の tgsvr に wwwroot が無く実際に起きた）。
    if not WWWROOT.is_dir():
        raise SystemExit(
            f"アセットが見つかりません: {WWWROOT}\n"
            "  このまま生成すると /Images/ と /favicon.ico が 404 になります。\n"
            "  リポジトリの WeatherStatic/wwwroot/ を配置するか、\n"
            "  環境変数 WEATHERCORE_WWWROOT で場所を指定してください。"
        )
    missing = [rel for rel in ASSET_PATHS if not (WWWROOT / rel).exists()]
    if missing:
        raise SystemExit(f"アセットが欠けています: {', '.join(missing)}（{WWWROOT} 配下）")
    for rel in ASSET_PATHS:
        src = WWWROOT / rel
        dst = PUBLIC / rel
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    print("  [assets] Images / favicon.ico / robots.txt をコピーしました")


def write(path_rel: str, html: str) -> None:
    out = PUBLIC / path_rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"  [html] {path_rel}  ({len(html):,} bytes)")


def season_period(now: datetime) -> tuple[datetime, datetime]:
    """記録の集計期間（開始, 終了=昨日）。夏=1/1 から、冬=寒候年の 8/1 から（winter_start）。"""
    start = datetime(now.year, 1, 1) if is_summer(now) else winter_start(now)
    end = datetime.combine(now.date() - timedelta(days=1), datetime.min.time())
    return start, end


def main_city_order(stations: dict) -> list[tuple[int, dict]]:
    """主要都市を旧サイトの表示順（北→南）で返す。"""
    order = {s["code"]: i for i, s in enumerate(MAIN_STATIONS)}   # 国際地点番号順
    mains = [(int(code), rec) for code, rec in stations["stations"].items()
             if rec.get("main")]
    mains.sort(key=lambda x: order.get(x[1]["intl"], 999))
    return mains


# ---------------------------------------------------------------- ページ: HighsMain

def build_highsmain(env: Environment, today: dict, meta: dict, fc: dict,
                    stations: dict, hist: History) -> None:
    now = datetime.fromisoformat(meta["source_time"])
    summer = is_summer(now)
    start, end = season_period(now)
    # 予報の対象日: 今日の最高気温予報があれば今日、無ければ（17 時発表以降）明日
    d_today = now.date().isoformat()
    d_tomorrow = (now.date() + timedelta(days=1)).isoformat()

    def pick_fc(code):
        sd = fc["stations"].get(str(code), {})
        f = sd.get(d_today)
        if f and f.get("tmax") is not None:
            return f, False
        return sd.get(d_tomorrow) or {}, True

    target_tomorrow = False
    cities = []
    for code, rec in main_city_order(stations):
        t = today.get(code, {})
        f, target_tomorrow = pick_fc(code)
        nml = normal_daily(code, "tmax", now.date())
        counts = hist.day_counts(rec["row"], start.date(), end.date())
        tmax = t.get("tmax")
        fc_tmax = f.get("tmax")
        if summer:
            season_val, season_date = t.get("year_tmax"), t.get("year_tmax_date")
        else:
            season_val, season_date = hist.extreme(
                "tmax", rec["row"], start.date(), end.date(), highest=False)
        cities.append({
            "name": rec["name"], "place": station_slug(rec),
            "tmax": tmax,
            "tmax_bg": bcolor(tmax - nml) if (tmax is not None and nml is not None) else "#FFFFFF",
            "fc_tmax": fc_tmax,
            "fc_bg": bcolor(fc_tmax * 10 - nml) if (fc_tmax is not None and nml is not None) else "#FFFFFF",
            "fc_weather": f.get("weather", "-"),
            "fc_wcode": f.get("wcode", "-"),
            "normal": nml,
            "season_val": season_val, "season_date": season_date,
            "counts": counts,
        })

    context = {
        "now": now,
        "hour0": now.hour == 0,
        "summer": summer,
        "season": is_season(now),
        "counts": meta["counts"],
        "target_tomorrow": target_tomorrow,
        "period_start": start,
        "period_end": end,
        "days_diff": (now - end).days,
        "cities": cities,
        "nav_active": "temperature",
        "page_title": "今日の最高気温 - 主要都市",
        "page_header": "今日の最高気温 - 主要都市",
        "build_year": now.year,
    }
    html = env.get_template("temperature/highsmain.html").render(**context)
    write("Temperature/HighsMain/index.html", html)


# ---------------------------------------------------------------- ページ: LowsMain

def build_lowsmain(env: Environment, today: dict, meta: dict, fc: dict,
                   stations: dict, hist: History) -> None:
    now = datetime.fromisoformat(meta["source_time"])
    summer = is_summer(now)
    start, end = season_period(now)
    # 最低気温予報の対象: 9 時前は「今朝」（今日）、以降は「明朝」（明日）
    morning = now.hour < 9 and now.hour != 0
    want = now.date() if morning else now.date() + timedelta(days=1)

    cities = []
    for code, rec in main_city_order(stations):
        t = today.get(code, {})
        sd = fc["stations"].get(str(code), {})
        f = sd.get(want.isoformat()) or {}
        if f.get("tmin") is None:   # 対象日の予報が無ければもう一方の日で補完
            other = now.date() + timedelta(days=1) if morning else now.date()
            f = sd.get(other.isoformat()) or f
        nml = normal_daily(code, "tmin", now.date())
        counts = hist.day_counts(rec["row"], start.date(), end.date())
        tmin = t.get("tmin")
        fc_tmin = f.get("tmin")
        if summer:   # 夏: 最低気温の最高（熱帯夜的な記録）
            season_val, season_date = hist.extreme(
                "tmin", rec["row"], start.date(), end.date(), highest=True)
        else:        # 冬: 今季の最低気温
            season_val, season_date = t.get("year_tmin"), t.get("year_tmin_date")
        cities.append({
            "name": rec["name"], "place": station_slug(rec),
            "tmin": tmin,
            "tmin_bg": bcolor(tmin - nml) if (tmin is not None and nml is not None) else "#FFFFFF",
            "fc_tmin": fc_tmin,
            "fc_bg": bcolor(fc_tmin * 10 - nml) if (fc_tmin is not None and nml is not None) else "#FFFFFF",
            "fc_weather": f.get("weather", "-"),
            "fc_wcode": f.get("wcode", "-"),
            "normal": nml,
            "season_val": season_val, "season_date": season_date,
            "counts": counts,
        })

    context = {
        "now": now,
        "hour0": now.hour == 0,
        "summer": summer,
        "season": is_season(now),
        "counts": meta["counts"],
        "morning": morning,
        "period_start": start,
        "period_end": end,
        "days_diff": (now - end).days,
        "cities": cities,
        "nav_active": "temperature",
        "page_title": "今朝の最低気温 - 主要都市",
        "page_header": "今朝の最低気温 - 主要都市",
        "build_year": now.year,
    }
    html = env.get_template("temperature/lowsmain.html").render(**context)
    write("Temperature/LowsMain/index.html", html)


# ---------------------------------------------------------------- ページ: Home

# トップページの主要都市（旧 topdata.json 相当。code = 国際地点番号）
HOME_CITIES = [47412, 47590, 47662, 47636, 47772, 47765, 47891, 47807, 47827, 47936]
# 札幌, 仙台, 東京, 名古屋, 大阪, 広島, 高松, 福岡, 鹿児島, 那覇


def load_current() -> dict | None:
    p = DATA / "current.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def build_home(env: Environment, today: dict, meta: dict, fc: dict,
               stations: dict, hist: History) -> None:
    now = datetime.fromisoformat(meta["source_time"])
    summer = is_summer(now)
    start, end = season_period(now)
    st = stations["stations"]
    current = load_current() or {"stations": {}}

    cities = []
    for code in HOME_CITIES:
        rec = st.get(str(code))
        t = today.get(code)
        if rec is None or t is None:
            continue
        nml_tmax = normal_daily(code, "tmax", now.date())
        nml_tmin = normal_daily(code, "tmin", now.date())
        c = hist.day_counts(rec["row"], start.date(), end.date())
        if summer:
            hs_val, hs_date = t.get("year_tmax"), t.get("year_tmax_date")
            ls_val, ls_date = hist.extreme("tmin", rec["row"], start.date(), end.date(), True)
        else:
            hs_val, hs_date = hist.extreme("tmax", rec["row"], start.date(), end.date(), False)
            ls_val, ls_date = t.get("year_tmin"), t.get("year_tmin_date")
        cur = current["stations"].get(str(code), {})
        cities.append({
            "code": code, "amedas": rec["amedas"],
            "lat": rec["lat"], "lon": rec["lon"],
            "name": rec["name"], "place": station_slug(rec),
            "cur_temp": cur.get("temp"),
            "cur_wthr": cur.get("wthr"),
            "cur_wcode": cur.get("wcode"),
            "tmax": t.get("tmax"),
            "tmax_bg": bcolor(t["tmax"] - nml_tmax)
                       if (t.get("tmax") is not None and nml_tmax is not None) else "#FFFFFF",
            "tmin": t.get("tmin"),
            "tmin_bg": bcolor(t["tmin"] - nml_tmin)
                       if (t.get("tmin") is not None and nml_tmin is not None) else "#FFFFFF",
            "normal_tmax": nml_tmax, "normal_tmin": nml_tmin,
            "hs_val": hs_val, "hs_date": hs_date,
            "ls_val": ls_val, "ls_date": ls_date,
            "counts": c,
        })

    # 東京の直近 31 日グラフ（Highcharts 用。値は ×10、欠測 null）
    tokyo = st.get("47662")
    g_end = (now - timedelta(days=1, hours=3)).date()
    g_start = g_end - timedelta(days=31)
    graph = {"start": g_start, "ht": [], "lt": [], "n_ht": [], "n_lt": []}
    if tokyo:
        graph["ht"] = hist.series("tmax", tokyo["row"], g_start, g_end)
        graph["lt"] = hist.series("tmin", tokyo["row"], g_start, g_end)
        nml = load_normals(47662) or {"daily": {}}
        d = g_start
        while d <= g_end:
            mo = nml["daily"].get(str(d.month), {})
            for key, elem in (("n_ht", "tmax"), ("n_lt", "tmin")):
                arr = mo.get(elem, [])
                graph[key].append(arr[d.day - 1] if d.day - 1 < len(arr) else None)
            d += timedelta(days=1)

    # 昨日の服装投票の集計（aggregate_votes.py が蓄積した votes_raw から）
    vote_summary = None
    try:
        import sqlite3
        vconn = sqlite3.connect(BASE / "store" / "weather.sqlite")
        y = (now.date() - timedelta(days=1)).isoformat()
        row = vconn.execute("SELECT COALESCE(SUM(v),0), COUNT(*) FROM votes_raw "
                            "WHERE date = ?", (y,)).fetchone()
        vconn.close()
        if row and row[1] > 0:
            vote_summary = {"yes": int(row[0]), "total": int(row[1]),
                            "pct": round(100 * row[0] / row[1])}
    except Exception:
        pass

    from weatherlib.svgchart import timeseries_svg
    graph_svg = timeseries_svg(
        "東京の過去30日間の気温の推移", g_start,
        [{"label": "最高気温", "color": "#F92500", "values": graph["ht"], "width": 1.6, "r": 2.2},
         {"label": "最低気温", "color": "#0C00CC", "values": graph["lt"], "width": 1.6, "r": 2.2},
         {"label": "最高気温平年値", "color": "#f5a898", "values": graph["n_ht"], "width": 1.2},
         {"label": "最低気温平年値", "color": "#9fa8e8", "values": graph["n_lt"], "width": 1.2}])

    context = {
        "now": now, "hour0": now.hour == 0,
        "summer": summer, "season": is_season(now),
        "counts": meta["counts"],
        "period_end": end, "days_diff": (now - end).days,
        "cities": cities,
        "live_cities": [{"code": c["code"], "amedas": c["amedas"],
                         "lat": c["lat"], "lon": c["lon"]} for c in cities],
        "graph": graph, "graph_svg": graph_svg,
        "vote_summary": vote_summary,
        "current_time": (datetime.fromisoformat(current["amedas_time"])
                         if current.get("amedas_time") else None),
        "wthr_time": (datetime.fromisoformat(current["wthr_time"])
                      if current.get("wthr_time") else None),
        "nav_active": "home",
        "page_title": "個人開発気象統計",
        "page_header": "個人開発気象統計",
        "build_year": now.year,
    }
    import os as _os
    html = env.get_template("home.html").render(
        vote_url=_os.environ.get("WEATHER_VOTE_URL", "/vote.gif"), **context)
    write("index.html", html)

    # トップページが 10 分ごとに読む現在値。fetch_data.py --current-only も
    # ここへ直接置くので、サイト再生成を待たずに更新が届く。
    src = BASE / "data" / "current.json"
    if src.exists():
        dst = PUBLIC / "data" / "current.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  [data] data/current.json → public/data/ ({src.stat().st_size:,} bytes)")


# ---------------------------------------------------------------- ページ: 各地一覧

REGION_ANCHORS = [("東北", 31), ("関東", 40), ("甲信", 48), ("東海", 50), ("北陸", 54),
                  ("近畿", 60), ("中国", 66), ("四国", 71), ("九州", 81), ("沖縄", 91)]

# 地方別フィルタ（prec_no 範囲 → 地方）
REGIONS = [("hokkaido", "北海道", 11, 24), ("tohoku", "東北", 31, 36),
           ("kanto", "関東", 40, 46), ("koshin", "甲信", 48, 49),
           ("tokai", "東海", 50, 53), ("hokuriku", "北陸", 54, 57),
           ("kinki", "近畿", 60, 65), ("chugoku", "中国", 66, 69),
           ("shikoku", "四国", 71, 74), ("kyushu", "九州", 81, 88),
           ("okinawa", "沖縄", 91, 94)]


def region_of(prec_no: int) -> str:
    for key, _, lo, hi in REGIONS:
        if lo <= prec_no <= hi:
            return key
    return "other"


def build_lists(env: Environment, today: dict, meta: dict,
                stations: dict, hist: History) -> None:
    now = datetime.fromisoformat(meta["source_time"])
    summer = is_summer(now)
    start, end = season_period(now)

    counts_all = hist.all_day_counts(start.date(), end.date())
    lo_tmax = hist.all_extremes("tmax", start.date(), end.date(), highest=False)
    hi_tmin = hist.all_extremes("tmin", start.date(), end.date(), highest=True)

    def row_counts(row: int) -> dict[str, int]:
        return {k: int(v[row]) if row < len(v) else 0 for k, v in counts_all.items()}

    def row_extreme(ex, row: int):
        if ex is None:
            return None, None
        vals, idx, has = ex
        if row >= len(has) or not has[row]:
            return None, None
        return int(vals[row]), start.date() + timedelta(days=int(idx[row]))

    # 都道府県（prec_no）でグループ化
    groups: dict[int, dict] = {}
    for code_s, rec in stations["stations"].items():
        code = int(code_s)
        t = today.get(code)
        if t is None or not rec["elements"]["temp"]:
            continue
        prec = rec.get("etrn", {}).get("prec_no", 99)
        g = groups.setdefault(prec, {"prec_no": prec, "pref": rec["pref"], "stations": []})
        nml_tmax = normal_daily(code, "tmax", now.date())
        nml_tmin = normal_daily(code, "tmin", now.date())
        c = row_counts(rec["row"])
        if summer:
            hs_val, hs_date = t.get("year_tmax"), t.get("year_tmax_date")
            ls_val, ls_date = row_extreme(hi_tmin, rec["row"])
        else:
            hs_val, hs_date = row_extreme(lo_tmax, rec["row"])
            ls_val, ls_date = t.get("year_tmin"), t.get("year_tmin_date")
        g["stations"].append({
            "amedas": rec["amedas"], "name": rec["name"],
            "place": station_slug(rec),
            "tmax": t.get("tmax"),
            "tmax_bg": bcolor(t["tmax"] - nml_tmax)
                       if (t.get("tmax") is not None and nml_tmax is not None) else "#FFFFFF",
            "tmin": t.get("tmin"),
            "tmin_bg": bcolor(t["tmin"] - nml_tmin)
                       if (t.get("tmin") is not None and nml_tmin is not None) else "#FFFFFF",
            "normal_tmax": nml_tmax, "normal_tmin": nml_tmin,
            "hs_val": hs_val, "hs_date": hs_date,     # 最高気温の記録（夏=今年最高/冬=日最高の最低）
            "ls_val": ls_val, "ls_date": ls_date,     # 最低気温の記録（夏=最低の最高/冬=今季最低）
            "counts": c,
        })

    ordered = [groups[k] for k in sorted(groups)]
    for g in ordered:
        g["region"] = region_of(g["prec_no"])
        g["stations"].sort(key=lambda s: s["amedas"])

    base_ctx = {
        "now": now, "hour0": now.hour == 0,
        "summer": summer, "season": is_season(now),
        "counts": meta["counts"],
        "period_start": start, "period_end": end,
        "days_diff": (now - end).days,
        "groups": ordered, "regions": REGION_ANCHORS,
        "region_filters": [(k, n) for k, n, _, _ in REGIONS],
        "nav_active": "temperature", "build_year": now.year,
    }
    html = env.get_template("temperature/highslist.html").render(
        **base_ctx, page_title="今日の最高気温 - 各地", page_header="今日の最高気温 - 各地")
    write("Temperature/HighsList/index.html", html)
    html = env.get_template("temperature/lowslist.html").render(
        **base_ctx, page_title="今朝の最低気温 - 各地", page_header="今朝の最低気温 - 各地")
    write("Temperature/LowsList/index.html", html)


# ---------------------------------------------------------------- ページ: 順位

def build_rankings(env: Environment, today: dict, meta: dict, stations: dict) -> None:
    now = datetime.fromisoformat(meta["source_time"])
    st = stations["stations"]

    def entries(field: str, descending: bool, threshold, min_rows: int = 50):
        rows = []
        for code_s, rec in st.items():
            t = today.get(int(code_s))
            if t is None or t.get(field) is None:
                continue
            rows.append((t[field], t.get(f"{field}_at") or "", rec))
        rows.sort(key=lambda x: x[0], reverse=descending)
        out, rank, prev = [], 0, None
        for i, (val, at, rec) in enumerate(rows, 1):
            qualifies = (val >= threshold) if descending else (val < threshold)
            if not qualifies and i > min_rows:
                break
            if val != prev:
                rank, prev = i, val
            out.append({
                "rank": rank,
                "pref": rec["pref"], "name": rec["name"],
                "place": station_slug(rec),
                "temp_str": f"{val / 10:.1f}", "at": at,
            })
        return out

    pages = [
        ("Temperature/TodayHighsDec", "今日の最高気温ランキング（気温の高い順）",
         "の最高気温が高い順に一覧にしています。", entries("tmax", True, 300),
         [("猛暑日（最高気温35℃以上）となった地点数", meta["counts"]["moushobi"]),
          ("真夏日（最高気温30℃以上）となった地点数", meta["counts"]["manatsubi"])]),
        ("Temperature/TodayHighsAsc", "今日の最高気温ランキング（気温の低い順）",
         "の最高気温が低い順に一覧にしています。", entries("tmax", False, 0),
         [("真冬日（最高気温が零度未満）となった地点数", meta["counts"]["mafuyubi"])]),
        ("Temperature/TodayLowsDec", "今朝の最低気温ランキング（気温の高い順）",
         "までの最低気温が25度以上（ほぼ熱帯夜に相当）の地点及び最低気温の高い上位50地点を気温の高い順にランキングしました。",
         entries("tmin", True, 250),
         [("今朝の最低気温が25℃以上の地点数", meta["counts"]["nettaiya"])]),
        ("Temperature/TodayLowsAsc", "今朝の最低気温ランキング（気温の低い順）",
         "の最低気温が低い順（冬日の地点が50地点より少ない場合は気温の低い順に50地点）に一覧にしています。",
         entries("tmin", False, 0),
         [("冬日（最低気温零度未満）となった地点数", meta["counts"]["fuyubi"])]),
    ]
    for path, header, explain, items, count_lines in pages:
        html = env.get_template("temperature/ranking.html").render(
            now=now, hour0=now.hour == 0,
            page_title=header, page_header=header,
            explain=explain, items=items, count_lines=count_lines,
            nav_active="temperature", build_year=now.year,
            path=path,
        )
        write(f"{path}/index.html", html)


# ---------------------------------------------------------------- ページ: 夏/冬ランキング

def build_season_pages(env: Environment, meta: dict, stations: dict, hist: History) -> None:
    """Summer/Winter の日数・気温のランキングと一覧（計 8 ページ）を nc の履歴から生成する。"""
    import numpy as np
    from weatherlib.ncstore import FILL

    now = datetime.fromisoformat(meta["source_time"])
    yesterday = datetime.combine(now.date() - timedelta(days=1), datetime.min.time())

    temp_st = [(int(c), r) for c, r in stations["stations"].items()
               if r["elements"]["temp"]]
    temp_st.sort(key=lambda x: (x[1].get("etrn", {}).get("prec_no", 99), x[1]["amedas"]))
    rows_idx = np.array([r["row"] for _, r in temp_st])

    def season(start: datetime):
        m = {v: hist.matrix(v, start.date(), yesterday.date())
             for v in ("tmax", "tmin", "tavg")}
        if m["tmax"] is None:
            return None
        period_days = (yesterday.date() - start.date()).days + 1
        if m["tmax"].shape[0] <= rows_idx.max():
            return None
        sel = {v: arr[rows_idx] for v, arr in m.items()}
        ok = {v: (a != FILL) for v, a in sel.items()}
        qual = ok["tmax"].sum(axis=1) >= period_days / 2   # 充足地点のみ集計

        def counts(v, th, ge=True):
            c = (((sel[v] >= th) if ge else (sel[v] < th)) & ok[v]).sum(axis=1)
            return np.where(qual, c, -1)

        def extreme(v, highest=True):
            has = ok[v].any(axis=1) & qual
            if highest:
                idx = np.where(ok[v], sel[v], -32768).argmax(axis=1)
            else:
                idx = np.where(ok[v], sel[v], 32767).argmin(axis=1)
            vals = sel[v][np.arange(len(rows_idx)), idx]
            return vals, idx, has

        return {"start": start, "end": yesterday, "days": period_days,
                "n_stations": int(qual.sum()), "counts": counts, "extreme": extreme}

    def rank_rows(values, valid, fmt):
        """上位 50 位まで（同値同順位）。"""
        import numpy as np
        order = np.argsort(-values, kind="stable")
        out, rank, prev, i = [], 0, None, 0
        for oi in order:
            if not valid[oi]:
                continue
            i += 1
            v = int(values[oi])
            if v != prev:
                rank, prev = i, v
            if rank > 50:
                break
            code, rec = temp_st[oi]
            out.append({"rank": rank, "name": rec["pref"] + rec["name"],
                        "place": station_slug(rec),
                        "val": fmt(v)})
        return out

    def pref_groups(make_station):
        groups = {}
        for i, (code, rec) in enumerate(temp_st):
            s = make_station(i, rec)
            if s is None:
                continue
            prec = rec.get("etrn", {}).get("prec_no", 99)
            g = groups.setdefault(prec, {"prec_no": prec, "pref": rec["pref"],
                                         "region": region_of(prec), "stations": []})
            g["stations"].append(s)
        return [groups[k] for k in sorted(groups)]

    def fmt_temp(v):
        return f"{v / 10:.1f}"

    def place_of(rec):
        return station_slug(rec)

    common = {"nav_active": "ranking", "build_year": now.year,
              "region_filters": [(k, n) for k, n, _, _ in REGIONS]}

    # ---------------- 夏（今年 1/1〜昨日） ----------------
    s = season(datetime(now.year, 1, 1))
    if s:
        subnav = [("/Summer/Ranking", "猛暑日日数ランキング"),
                  ("/Summer/SummerDayList", "猛暑日の日数一覧"),
                  ("/Summer/Hottest", "最高気温ランキング"),
                  ("/Summer/HottestList", "最高気温一覧")]
        def nav(active):
            return [(u, l, l == active) for u, l in subnav]
        cnt = {"moushobi": s["counts"]("tmax", 350), "manatsubi": s["counts"]("tmax", 300),
               "tavg30": s["counts"]("tavg", 300), "nettaiya": s["counts"]("tmin", 250)}
        info_days = (f"気象庁の観測所のうち気温を測定している {s['n_stations']} カ所を対象に、"
                     f"{now.year} 年の観測記録を使用して、猛暑日（最高気温が35度以上）の日数、"
                     "真夏日（最高気温が30度以上）の日数、平均気温が30度以上の日数、"
                     "最低気温が25度以上（熱帯夜にほぼ相当）の日数を集計し上位50位までをリストにしたものです。")
        write("Summer/Ranking/index.html", env.get_template("season/ranking.html").render(
            **common, page_title=f"{now.year}年夏 猛暑日、真夏日等の日数のランキング",
            page_header=f"{now.year}年夏 猛暑日、真夏日等の日数のランキング",
            subnav=nav("猛暑日日数ランキング"),
            period_start=s["start"], period_end=s["end"], n_stations=s["n_stations"],
            info_text=info_days,
            tables=[
                {"title": "猛暑日の日数", "kind": "days",
                 "rows": rank_rows(cnt["moushobi"], cnt["moushobi"] > 0, lambda v: v)},
                {"title": "真夏日の日数", "kind": "days",
                 "rows": rank_rows(cnt["manatsubi"], cnt["manatsubi"] > 0, lambda v: v)},
                {"title": "平均気温30度以上の日数", "kind": "days",
                 "rows": rank_rows(cnt["tavg30"], cnt["tavg30"] > 0, lambda v: v)},
                {"title": "最低気温25度以上の日数", "kind": "days",
                 "rows": rank_rows(cnt["nettaiya"], cnt["nettaiya"] > 0, lambda v: v)},
            ]))

        write("Summer/SummerDayList/index.html", env.get_template("season/daylist.html").render(
            **common, page_title=f"{now.year}年夏 猛暑日等の日数一覧",
            page_header=f"{now.year}年夏 猛暑日等の日数一覧",
            subnav=nav("猛暑日の日数一覧"),
            period_start=s["start"], period_end=s["end"], n_stations=s["n_stations"],
            info_text=info_days.replace("上位50位までをリストにしたものです", "一覧にしたものです"),
            col_headers=["猛暑日", "真夏日", "平均気温<br />30度以上", "最低気温<br />25度以上"],
            groups=pref_groups(lambda i, rec: {
                "name": rec["name"], "place": place_of(rec),
                "counts": [int(cnt["moushobi"][i]), int(cnt["manatsubi"][i]),
                           int(cnt["tavg30"][i]), int(cnt["nettaiya"][i])],
            } if cnt["moushobi"][i] >= 0 else None)))

        ex = {v: s["extreme"](v, highest=True) for v in ("tmax", "tavg", "tmin")}
        info_temp = (f"気象庁の観測所のうち気温を測定している {s['n_stations']} カ所を対象に、"
                     f"{now.year} 年の観測記録を集計して、日最高気温、日平均気温、日最低気温の"
                     "高い順に上位50位までをリストにしました。")
        write("Summer/Hottest/index.html", env.get_template("season/ranking.html").render(
            **common, page_title=f"{now.year}年夏 最高気温、平均気温のランキング",
            page_header=f"{now.year}年夏 最高気温、平均気温のランキング",
            subnav=nav("最高気温ランキング"),
            period_start=s["start"], period_end=s["end"], n_stations=s["n_stations"],
            info_text=info_temp,
            tables=[
                {"title": "日最高気温の最高", "kind": "temp",
                 "rows": rank_rows(ex["tmax"][0], ex["tmax"][2], fmt_temp)},
                {"title": "日平均気温の最高", "kind": "temp",
                 "rows": rank_rows(ex["tavg"][0], ex["tavg"][2], fmt_temp)},
                {"title": "日最低気温の最高", "kind": "temp",
                 "rows": rank_rows(ex["tmin"][0], ex["tmin"][2], fmt_temp)},
            ]))

        def hot_pairs(i, rec):
            if not ex["tmax"][2][i]:
                return None
            pairs = []
            for v in ("tmax", "tavg", "tmin"):
                vals, idx, has = ex[v]
                pairs.append((fmt_temp(int(vals[i])) if has[i] else "-",
                              s["start"].date() + timedelta(days=int(idx[i])) if has[i] else None))
            return {"name": rec["name"], "place": place_of(rec), "pairs": pairs}

        write("Summer/HottestList/index.html", env.get_template("season/extremelist.html").render(
            **common, page_title=f"{now.year}年夏 各地の最高気温の一覧",
            page_header=f"{now.year}年夏 各地の最高気温の一覧",
            subnav=nav("最高気温一覧"),
            period_start=s["start"], period_end=s["end"], n_stations=s["n_stations"],
            info_text=info_temp.replace("上位50位までをリストにしました", "地点ごとに一覧にしました"),
            pair_headers=["最高気温の最高", "平均気温の最高", "最低気温の最高"],
            groups=pref_groups(hot_pairs)))

    # ---------------- 冬（寒候年: 8/1〜昨日） ----------------
    wy_start = winter_start(now)
    wy_from = f"{wy_start.year}年{wy_start.month}月{wy_start.day}日"
    w = season(wy_start)
    if w:
        wyear = wy_start.year + 1
        subnav = [("/Winter/Ranking", "冬日日数ランキング"),
                  ("/Winter/WinterDayList", "冬日の日数一覧"),
                  ("/Winter/Coldest", "最低気温ランキング"),
                  ("/Winter/LowestList", "最低気温一覧")]
        def wnav(active):
            return [(u, l, l == active) for u, l in subnav]
        cnt = {"fuyubi": w["counts"]("tmin", 0, ge=False),
               "tavg0": w["counts"]("tavg", 0, ge=False),
               "mafuyubi": w["counts"]("tmax", 0, ge=False)}
        info_days = (f"気象庁の観測所のうち気温を測定している {w['n_stations']} カ所を対象に、"
                     f"{wy_from}からの観測記録を集計して、冬日（最低気温が0度未満）の日数、"
                     "平均気温が0度未満の日数、真冬日（最高気温が0度未満）の日数の上位50位までをリストにしました。")
        write("Winter/Ranking/index.html", env.get_template("season/ranking.html").render(
            **common, page_title=f"{wyear}年冬 冬日、真冬日等の日数のランキング",
            page_header=f"{wyear}年冬 冬日、真冬日等の日数のランキング",
            subnav=wnav("冬日日数ランキング"),
            period_start=w["start"], period_end=w["end"], n_stations=w["n_stations"],
            info_text=info_days,
            tables=[
                {"title": "冬日の日数", "kind": "days",
                 "rows": rank_rows(cnt["fuyubi"], cnt["fuyubi"] > 0, lambda v: v)},
                {"title": "平均気温0度未満の日数", "kind": "days",
                 "rows": rank_rows(cnt["tavg0"], cnt["tavg0"] > 0, lambda v: v)},
                {"title": "真冬日の日数", "kind": "days",
                 "rows": rank_rows(cnt["mafuyubi"], cnt["mafuyubi"] > 0, lambda v: v)},
            ]))

        write("Winter/WinterDayList/index.html", env.get_template("season/daylist.html").render(
            **common, page_title=f"{wyear}年冬 冬日等の日数一覧",
            page_header=f"{wyear}年冬 冬日等の日数一覧",
            subnav=wnav("冬日の日数一覧"),
            period_start=w["start"], period_end=w["end"], n_stations=w["n_stations"],
            info_text=info_days.replace("上位50位までをリストにしました", "一覧にしました"),
            col_headers=["冬日<br />（最低気温0度未満）", "平均気温0度未満", "真冬日<br />（最高気温0度未満）"],
            groups=pref_groups(lambda i, rec: {
                "name": rec["name"], "place": place_of(rec),
                "counts": [int(cnt["fuyubi"][i]), int(cnt["tavg0"][i]), int(cnt["mafuyubi"][i])],
            } if cnt["fuyubi"][i] >= 0 else None)))

        exw = {v: w["extreme"](v, highest=False) for v in ("tmin", "tavg", "tmax")}
        info_temp = (f"気象庁の観測所のうち気温を測定している {w['n_stations']} カ所を対象に、"
                     f"{wy_from}からの観測記録を集計して、日最低気温、日平均気温、日最高気温の"
                     "低い順に上位50位までをリストにしました。")
        write("Winter/Coldest/index.html", env.get_template("season/ranking.html").render(
            **common, page_title=f"{wyear}年冬 最低気温、平均気温のランキング",
            page_header=f"{wyear}年冬 最低気温、平均気温のランキング",
            subnav=wnav("最低気温ランキング"),
            period_start=w["start"], period_end=w["end"], n_stations=w["n_stations"],
            info_text=info_temp,
            tables=[
                {"title": "日最低気温の最低", "kind": "temp",
                 "rows": rank_rows(-exw["tmin"][0], exw["tmin"][2], lambda v: fmt_temp(-v))},
                {"title": "日平均気温の最低", "kind": "temp",
                 "rows": rank_rows(-exw["tavg"][0], exw["tavg"][2], lambda v: fmt_temp(-v))},
                {"title": "日最高気温の最低", "kind": "temp",
                 "rows": rank_rows(-exw["tmax"][0], exw["tmax"][2], lambda v: fmt_temp(-v))},
            ]))

        def cold_pairs(i, rec):
            if not exw["tmin"][2][i]:
                return None
            pairs = []
            for v in ("tmin", "tavg", "tmax"):
                vals, idx, has = exw[v]
                pairs.append((fmt_temp(int(vals[i])) if has[i] else "-",
                              w["start"].date() + timedelta(days=int(idx[i])) if has[i] else None))
            return {"name": rec["name"], "place": place_of(rec), "pairs": pairs}

        write("Winter/LowestList/index.html", env.get_template("season/extremelist.html").render(
            **common, page_title=f"{wyear}年冬 各地の最低気温の一覧",
            page_header=f"{wyear}年冬 各地の最低気温の一覧",
            subnav=wnav("最低気温一覧"),
            period_start=w["start"], period_end=w["end"], n_stations=w["n_stations"],
            info_text=info_temp.replace("上位50位までをリストにしました", "地点ごとに一覧にしました"),
            pair_headers=["最低気温の最低", "平均気温の最低", "最高気温の最低"],
            groups=pref_groups(cold_pairs)))


# ---------------------------------------------------------------- ページ: 雨温図

_CLIMATE_TARGETS: list | None = None


def legacy_slugs() -> dict[str, str]:
    """地点番号 → 旧サイト（weather.time-j.net の WeatherCore）の URL 名。

    新しいサイトは同じドメインで旧サイトを置き換えるので、/Stations/JP/Abashiri・
    /Climate/Chart/Abashiri のような旧 URL をそのまま使う（リンクや検索結果を
    切らない）。対応は legacy_slugs.py が旧サイトから取ってリポジトリに残した。"""
    p = BASE / "legacy" / "station_slugs.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))["slugs"]


def climate_targets(stations: dict) -> list:
    """月別平年値（気温・降水量）が 12 か月そろっている地点と一意 slug。

    Climate（雨温図）と Stations/JP（観測所ページ）が同じ slug 空間を
    共有するための単一の真実。slug は旧サイトの URL 名があればそれ、無ければ
    英語名の小文字。slug の衝突解決は挿入順に依存するので、候補集合と並び順を
    ここで固定する。旧 URL 名は必ずそのまま使えるよう、先に予約しておく。
    結果はプロセス内でキャッシュ。
    """
    global _CLIMATE_TARGETS
    if _CLIMATE_TARGETS is not None:
        return _CLIMATE_TARGETS
    targets = []
    legacy = legacy_slugs()
    slugs_seen: dict[str, int] = {s: 1 for s in legacy.values()}
    st_items = [(int(c), r) for c, r in stations["stations"].items()]
    st_items.sort(key=lambda x: (x[1].get("etrn", {}).get("prec_no", 99),
                                 not x[1].get("intl"), x[1]["amedas"]))
    for code, rec in st_items:
        nml = load_normals(code)
        if nml is None:
            continue
        mo = nml.get("monthly", {})
        if not mo.get("tavg") or any(v is None for v in mo["tavg"]) \
           or not mo.get("precip") or any(v is None for v in mo["precip"]):
            continue
        if str(code) in legacy:
            slug = legacy[str(code)]                       # 旧サイトと同じ URL
        else:
            base = (rec.get("place") or rec.get("en") or str(code)).lower().replace(" ", "-")
            n = slugs_seen.get(base, 0)
            slugs_seen[base] = n + 1
            slug = base if n == 0 else f"{base}-{code}"   # 同名ローマ字の衝突は code で区別
        targets.append((code, rec, nml, slug))
        _SLUG_BY_AMEDAS[str(rec.get("amedas"))] = slug
    _CLIMATE_TARGETS = targets
    return targets


def build_climate(env: Environment, stations: dict) -> None:
    """雨温図: 一覧＋地点ごとのグラフページ＋比較用データ JSON（平年値マスターから生成）。"""
    targets = climate_targets(stations)
    print(f"  [climate] 対象 {len(targets)} 地点")
    if not targets:
        return

    def disp(vals):
        return [round(v / 10, 1) if v is not None else None for v in vals]

    prec_names = {}
    by_prec: dict[int, list] = {}
    charts = []
    for code, rec, nml, slug in targets:
        prec = rec.get("etrn", {}).get("prec_no", 99)
        prec_names.setdefault(prec, rec["pref"])
        mo = nml["monthly"]
        year = mo.get("year", {})
        etrn = rec.get("etrn", {})
        php = "nml_sfc_ym" if rec.get("intl") else "nml_amd_ym"
        st = {
            "slug": slug, "name": rec["name"], "pref": rec["pref"],
            "prec_no": prec, "prec_name": rec["pref"],
            "amedas": rec["amedas"], "is_kansho": bool(rec.get("intl")),
            "year_tavg": (f"{year['tavg'] / 10:.1f}" if year.get("tavg") is not None else "-"),
            "year_precip": (f"{year['precip'] / 10:.1f}" if year.get("precip") is not None else "-"),
            "period": "1991〜2020年",
            "etrn_url": (f"https://www.data.jma.go.jp/stats/etrn/view/{php}.php"
                         f"?prec_no={etrn.get('prec_no', '')}&block_no={etrn.get('block_no', '')}"
                         "&year=&month=&day=&view="),
            "monthly": {
                "tmax": disp(mo.get("tmax") or [None] * 12),
                "tavg": disp(mo["tavg"]),
                "tmin": disp(mo.get("tmin") or [None] * 12),
                "precip": disp(mo["precip"]),
            },
        }
        charts.append(st)
        by_prec.setdefault(prec, []).append(
            {"slug": slug, "name": rec["name"], "k": st["is_kansho"]})

    # 比較用データ JSON と セレクタ用 index
    data_dir = PUBLIC / "data" / "climate"
    data_dir.mkdir(parents=True, exist_ok=True)
    for st in charts:
        (data_dir / f"{st['slug']}.json").write_text(
            json.dumps(st, ensure_ascii=False), encoding="utf-8")
    index = {str(p): {"name": prec_names[p], "stations": by_prec[p]}
             for p in sorted(by_prec)}
    (data_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")

    # 主要都市リンク（雨温図ページ間の移動用）
    main_links = [(slug, rec["name"]) for code, rec, nml, slug in targets
                  if rec.get("main")]

    # 一覧ページ
    groups = [{"prec_no": p, "pref": prec_names[p], "region": region_of(p),
               "stations": by_prec[p]} for p in sorted(by_prec)]
    for g in groups:
        g["stations"] = [{"slug": s["slug"], "name": s["name"], "is_kansho": s["k"]}
                         for s in g["stations"]]
    html = env.get_template("climate/index.html").render(
        page_title="雨温図（気温と降水量のグラフ）の観測地点一覧",
        nav_active="climate", build_year=datetime.now().year,
        region_filters=[(k, n) for k, n, _, _ in REGIONS],
        groups=groups)
    write("Climate/index.html", html)

    # 地点ページ
    kennai_map = {p: [{"slug": s["slug"], "name": s["name"], "is_kansho": s["k"]}
                      for s in by_prec[p]] for p in by_prec}
    from weatherlib.svgchart import uonzu_svg
    n = 0
    for st in charts:
        html = env.get_template("climate/chart.html").render(
            page_title=f"{st['pref']}{st['name']}の気候（気温と降水量のグラフ（雨温図））",
            nav_active="climate", build_year=datetime.now().year,
            st=st, st_svg=uonzu_svg(st["name"], st["monthly"]),
            kennai=kennai_map[st["prec_no"]], main_links=main_links)
        out = PUBLIC / "Climate" / "Chart" / st["slug"] / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        n += 1
    print(f"  [html] Climate/Chart/*  ({n} 地点)")


# ---------------------------------------------------------------- ページ: 観測地点 (Stations)

def _disp10(v) -> str:
    return f"{v / 10:.1f}" if v is not None else "-"


def build_stations(env: Environment, stations: dict, hist: History) -> None:
    """観測地点: 都道府県別一覧 + 地点ごとの観測所情報ページ (Stations/JP)。"""
    targets = [(c, r, n, s) for c, r, n, s in climate_targets(stations)
               if r["elements"].get("temp")]
    if not targets:
        return

    prec_names: dict[int, str] = {}
    by_prec: dict[int, list] = {}
    for code, rec, nml, slug in targets:
        prec = rec.get("etrn", {}).get("prec_no", 99)
        prec_names.setdefault(prec, rec["pref"])
        by_prec.setdefault(prec, []).append(
            {"slug": slug, "name": rec["name"], "is_kansho": bool(rec.get("intl"))})

    groups = [{"prec_no": p, "pref": prec_names[p], "region": region_of(p),
               "stations": by_prec[p]} for p in sorted(by_prec)]
    write("Stations/index.html", env.get_template("stations/index.html").render(
        page_title="気温の観測地点一覧（観測所情報）",
        nav_active="station", build_year=datetime.now().year,
        region_filters=[(k, n) for k, n, _, _ in REGIONS], groups=groups))

    # 地点ページ: 直近 30 日の実測 vs 平年値
    from weatherlib.svgchart import timeseries_svg
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=29)
    n_days = 30
    n_pages = 0
    for code, rec, nml, slug in targets:
        obs = {v: hist.series(v, rec["row"], start, end) for v in ("tmax", "tavg", "tmin")}
        nml_series: dict[str, list] = {"tmax": [], "tmin": []}
        for i in range(n_days):
            d = start + timedelta(days=i)
            md = nml["daily"].get(str(d.month), {})
            for v in nml_series:
                arr = md.get(v)
                nml_series[v].append(arr[d.day - 1] if arr and len(arr) >= d.day else None)
        svg = timeseries_svg(f"{rec['name']} 直近30日の気温", start, [
            {"color": "#f5b7a8", "values": nml_series["tmax"], "dash": "5 4", "width": 1.2},
            {"color": "#aab4f0", "values": nml_series["tmin"], "dash": "5 4", "width": 1.2},
            {"color": "#F92500", "values": obs["tmax"], "width": 2, "r": 2},
            {"color": "#008000", "values": obs["tavg"], "width": 1.5},
            {"color": "#0C00CC", "values": obs["tmin"], "width": 2, "r": 2},
        ], width=720, height=320)
        has_obs = any(x is not None for s in obs.values() for x in s)

        mo = nml["monthly"]
        year = mo.get("year", {})
        etrn = rec.get("etrn", {})
        php = "nml_sfc_ym" if rec.get("intl") else "nml_amd_ym"
        st = {
            "slug": slug, "name": rec["name"], "kana": rec.get("kana", ""),
            "en": rec.get("en", ""), "pref": rec["pref"],
            "is_kansho": bool(rec.get("intl")), "intl": rec.get("intl"),
            "amedas": rec["amedas"], "lat": rec.get("lat"), "lon": rec.get("lon"),
            "alt": rec.get("alt"), "main": rec.get("main"),
            "year_tavg": _disp10(year.get("tavg")), "year_tmax": _disp10(year.get("tmax")),
            "year_tmin": _disp10(year.get("tmin")), "year_precip": _disp10(year.get("precip")),
            "year_sun": _disp10(year.get("sun")),
            "elements": rec.get("elements", {}),
            "etrn_url": (f"https://www.data.jma.go.jp/stats/etrn/view/{php}.php"
                         f"?prec_no={etrn.get('prec_no', '')}&block_no={etrn.get('block_no', '')}"
                         "&year=&month=&day=&view="),
            "gsi_url": (f"https://maps.gsi.go.jp/#13/{rec.get('lat')}/{rec.get('lon')}/"
                        if rec.get("lat") else None),
            "monthly_rows": [
                {"m": m + 1,
                 "tmax": _disp10((mo.get("tmax") or [None] * 12)[m]),
                 "tavg": _disp10((mo.get("tavg") or [None] * 12)[m]),
                 "tmin": _disp10((mo.get("tmin") or [None] * 12)[m]),
                 "precip": _disp10((mo.get("precip") or [None] * 12)[m]),
                 "sun": _disp10((mo.get("sun") or [None] * 12)[m])}
                for m in range(12)],
        }
        html = env.get_template("stations/jp.html").render(
            page_title=f"{st['pref']} {st['name']}の気温、降水量、観測所情報",
            nav_active="station", build_year=datetime.now().year,
            st=st, st_svg=svg, has_obs=has_obs,
            period_label=f"{start.month}/{start.day}〜{end.month}/{end.day}",
            kennai=by_prec[rec.get("etrn", {}).get("prec_no", 99)])
        out = PUBLIC / "Stations" / "JP" / slug / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        n_pages += 1
    print(f"  [html] Stations/JP/*  ({n_pages} 地点)")


# ---------------------------------------------------------------- ページ: 月別気温ランキング (Monthly)

def build_monthly(env: Environment, stations: dict, hist: History) -> None:
    """月別気温ランキング: 平年値 12 か月 × 高低 + 実測の直近月。"""
    targets = [(c, r, n, s) for c, r, n, s in climate_targets(stations)
               if r["elements"].get("temp")]
    if not targets:
        return
    top_n = 60

    def ranking(var: str, m: int, low: bool) -> list[dict]:
        rows = []
        for code, rec, nml, slug in targets:
            vals = nml["monthly"].get(var)
            if not vals or vals[m] is None:
                continue
            rows.append((vals[m], rec, slug))
        rows.sort(key=lambda x: x[0], reverse=not low)
        out, prev, rank = [], None, 0
        for i, (v, rec, slug) in enumerate(rows[:top_n], 1):
            rank = rank if v == prev else i    # 同値は同順位
            prev = v
            out.append({"rank": rank, "name": rec["name"], "pref": rec["pref"],
                        "slug": slug, "value": _disp10(v)})
        return out

    # 平年値ランキング 12 か月 × 高低
    for m in range(12):
        for low in (False, True):
            html = env.get_template("monthly/heinenti.html").render(
                page_title=f"{m + 1}月の気温ランキング（平年値・{'低い順' if low else '高い順'}）",
                nav_active="monthly", build_year=datetime.now().year,
                month=m + 1, low=low,
                tables=[("最高気温", ranking("tmax", m, low)),
                        ("平均気温", ranking("tavg", m, low)),
                        ("最低気温", ranking("tmin", m, low))])
            write(f"Monthly/Heinenti{m + 1:02d}{'l' if low else ''}/index.html", html)

    # 実測の直近完結月（nc の日別値から月平均を計算）
    first_this = date.today().replace(day=1)
    obs_end = first_this - timedelta(days=1)
    obs_start = obs_end.replace(day=1)
    obs_label = f"{obs_start.year}年{obs_start.month}月"
    obs_tables, obs_n_stations = [], 0
    import numpy as np
    from weatherlib.ncstore import FILL
    row_map = {r["row"]: (int(c), r) for c, r in stations["stations"].items()}
    slug_map = {c: s for c, r, n, s in targets}
    need = (obs_end - obs_start).days + 1
    for var, label in (("tmax", "最高気温"), ("tavg", "平均気温"), ("tmin", "最低気温")):
        mat = hist.matrix(var, obs_start, obs_end)
        if mat is None:
            continue
        ok = mat != FILL
        valid = ok.sum(axis=1)
        means = np.where(ok, mat, 0).sum(axis=1) / np.maximum(valid, 1)
        rows = []
        for row_i in np.where(valid >= need - 2)[0]:   # 欠測 2 日まで許容
            code, rec = row_map.get(int(row_i), (None, None))
            if rec is None or code not in slug_map:
                continue
            rows.append((float(means[row_i]), rec, slug_map[code]))
        rows.sort(key=lambda x: x[0], reverse=True)
        obs_n_stations = max(obs_n_stations, len(rows))
        obs_tables.append((label, [
            {"rank": i, "name": rec["name"], "pref": rec["pref"], "slug": slug,
             "value": f"{v / 10:.1f}"}
            for i, (v, rec, slug) in enumerate(rows[:top_n], 1)]))
    if obs_n_stations:
        write("Monthly/Latest/index.html", env.get_template("monthly/observed.html").render(
            page_title=f"{obs_label}の気温ランキング（実測）",
            nav_active="monthly", build_year=datetime.now().year,
            obs_label=obs_label, n_stations=obs_n_stations, tables=obs_tables))

    # ハブ
    write("Monthly/index.html", env.get_template("monthly/index.html").render(
        page_title="月別の気温ランキング",
        nav_active="monthly", build_year=datetime.now().year,
        months=list(range(1, 13)), this_month=date.today().month,
        obs_label=obs_label if obs_n_stations else None,
        obs_n_stations=obs_n_stations))


# ---------------------------------------------------------------- ページ: 降水量ランキング (Precipitation)

def build_precipitation(env: Environment, stations: dict) -> None:
    """年降水量（平年値）ランキング。降水量平年値のある全地点。"""
    rows = []
    for code, rec, nml, slug in climate_targets(stations):
        v = nml["monthly"].get("year", {}).get("precip")
        if v is None:
            continue
        prec = rec.get("etrn", {}).get("prec_no", 99)
        rows.append({"value": v, "name": rec["name"], "pref": rec["pref"],
                     "slug": slug if rec["elements"].get("temp") else None,
                     "region": region_of(prec),
                     "is_kansho": bool(rec.get("intl"))})
    if not rows:
        return
    rows.sort(key=lambda r: r["value"], reverse=True)
    vmax = rows[0]["value"]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["disp"] = f"{r['value'] / 10:,.1f}"
        r["bar"] = round(100 * r["value"] / vmax, 1)
    write("Precipitation/index.html", env.get_template("precipitation/index.html").render(
        page_title="年降水量（平年値）ランキング",
        nav_active="precipitation", build_year=datetime.now().year,
        region_filters=[(k, n) for k, n, _, _ in REGIONS],
        top=rows[:3], rows=rows, least=list(reversed(rows[-30:])),
        n_stations=len(rows)))


def build_forecast(env: Environment) -> None:
    """数値予報チャート（旧 Gfs 後継）。画像は weather/tools/publish_charts.py が
    生成して R2 に置く。ページは latest.json を 1 回 fetch するだけ。"""
    import os
    charts_base = os.environ.get("WEATHER_CHARTS_BASE", "/charts")
    write("Forecast/index.html", env.get_template("forecast/index.html").render(
        page_title="数値予報チャート（ECMWF・GFS・アンサンブル降水）",
        nav_active="gfs", build_year=datetime.now().year,
        charts_base=charts_base))
    # 旧 URL からの誘導（Cloudflare Pages は _redirects、それ以外は meta refresh）
    write("Gfs/index.html",
          '<!doctype html><meta http-equiv="refresh" content="0; url=/Forecast/">'
          '<a href="/Forecast/">数値予報チャートへ移動</a>')


def build_app(env: Environment) -> None:
    """AIseed Weather（Flet アプリ）の紹介と開発マニュアル。観測データを
    使わないので fetch 層に依存せず、テンプレートだけで描ける。

    普通の人にはデスクトップ版を勧める位置づけなので、アプリの文書は
    このサイトが載せる。内容の正本はリポジトリの README / AGENTS / CLAUDE。"""
    write("App/index.html", env.get_template("app/index.html").render(
        page_title="AIseed Weather — 天気図スタジオ",
        nav_active="app", build_year=datetime.now().year))
    write("App/Develop/index.html", env.get_template("app/develop.html").render(
        page_title="AIseed Weather 開発マニュアル",
        nav_active="app", build_year=datetime.now().year))


def build_about(env: Environment) -> None:
    """このサイトについて。非公式であること・防災情報を扱わないことを明示する。"""
    write("About/index.html", env.get_template("about.html").render(
        page_title="このサイトについて — 個人開発気象統計",
        nav_active="about", build_year=datetime.now().year))


AMEDAS_SRC = BASE / "public_amedas"          # fetch_amedas_mirror.py の出力
AMEDAS_URL = "Data/AMeDAS"


def _half_month(d: date) -> tuple[date, date]:
    """その日が属する半月（1〜15 日 / 16 日〜月末）の初日と末日。"""
    if d.day <= 15:
        return d.replace(day=1), d.replace(day=15)
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return d.replace(day=16), nxt - timedelta(days=1)


def build_data_amedas(env: Environment) -> None:
    """アメダス 10 分値アーカイブ（半月ごとの NetCDF）を配る。

    気象庁の 10 分値は約 9 日で消える。fetch_amedas_mirror.py が半月ごとに
    封入した NetCDF を、説明ページ・機械向けの索引・地点一覧と一緒に
    /Data/AMeDAS/ に置く。生の 10 分値 JSON（map/）は気象庁が配っている
    期間と重なるので配らない。

    一覧と変数の表は archive/ の実物から作る（書き写すとずれに気づけない）。
    欠けた半月（収集が止まっていた期間）も一覧に出し、黙って飛ばさない。
    """
    out = PUBLIC / AMEDAS_URL
    src_index = AMEDAS_SRC / "index.json"
    meta = json.loads(src_index.read_text(encoding="utf-8")) if src_index.is_file() else {}
    archive = sorted(meta.get("archive", []), key=lambda a: a["start"])
    hourly = {h["start"]: h for h in meta.get("hourly", [])}

    def fmt_size(n: int) -> str:
        return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} KB"

    # NetCDF は大きく増えていくので、同じ大きさ・時刻のものは写し直さない
    n_copied = 0
    for a in archive + list(hourly.values()):
        s, d = AMEDAS_SRC / a["path"], out / a["path"]
        if not s.is_file():
            continue
        if d.is_file() and d.stat().st_size == s.stat().st_size \
                and int(d.stat().st_mtime) == int(s.stat().st_mtime):
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        n_copied += 1
    # 地点別にしか無い要素の日別（月ごとの NetCDF）。進行中の月は毎日作り直される
    daily = []
    for src in sorted((AMEDAS_SRC / "daily").glob("*/*.nc")):
        rel = src.relative_to(AMEDAS_SRC).as_posix()
        dst = out / rel
        if not (dst.is_file() and dst.stat().st_size == src.stat().st_size
                and int(dst.stat().st_mtime) == int(src.stat().st_mtime)):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n_copied += 1
        days = sorted((AMEDAS_SRC / "daily" / src.parent.name).glob(
            f"{src.parent.name}{src.stem}*.json"))
        size = src.stat().st_size
        daily.append({"path": rel, "month": f"{src.parent.name}-{src.stem}",
                      "days": len(days), "first": days[0].stem if days else "",
                      "last": days[-1].stem if days else "", "bytes": size,
                      "size": f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024
                              else f"{size / 1024:.0f} KB"})
    daily_vars = []
    if daily:
        import netCDF4
        with netCDF4.Dataset(AMEDAS_SRC / daily[-1]["path"]) as ds:
            for name, v in ds.variables.items():
                if name in ("station_id", "day") or name.endswith("_q"):
                    continue
                daily_vars.append({"name": name, "units": getattr(v, "units", ""),
                                   "desc": getattr(v, "long_name", ""),
                                   "scale": f"{float(getattr(v, 'scale_factor', 1.0)):g}"})

    st_src = AMEDAS_SRC / "station" / "index.json"
    n_stations = 0
    if st_src.is_file():
        (out / "station").mkdir(parents=True, exist_ok=True)
        shutil.copy2(st_src, out / "station" / "index.json")

    # 欠けも含めた半月の並び（最初の封入から、終わっている最後の半月まで）。
    # ミラーは半月の最終日が取得窓から外れてから封入するので、それまでの期間は
    # 「欠け」ではなく「まとめる前」（封入の予定日を出す）
    window = int(meta.get("fetch_window_days", 10))
    periods = []
    if archive:
        have = {a["start"]: a for a in archive}
        last_done = _half_month(date.today())[0] - timedelta(days=1)
        d = date.fromisoformat(archive[0]["start"])
        while d <= last_done:
            first, last = _half_month(d)
            a = have.get(first.isoformat())
            expected = ((last - first).days + 1) * 144
            if a:
                h = hourly.get(a["start"])
                periods.append({"start": a["start"], "end": a["end"], "path": a["path"],
                                "slots": a["slots"], "expected": expected,
                                "size": fmt_size(a["bytes"]),
                                "hourly": h["path"] if h else None,
                                "hourly_size": fmt_size(h["bytes"]) if h else ""})
            else:
                due = last + timedelta(days=window + 1)
                periods.append({"start": first.isoformat(), "end": last.isoformat(),
                                "path": None,
                                "pending": f"{due.month}月{due.day}日" if due >= date.today() else None})
            d = last + timedelta(days=1)

    # 変数の表は最新のファイルの属性から
    variables, example = [], "08-2.nc"
    if archive and (AMEDAS_SRC / archive[-1]["path"]).is_file():
        import netCDF4
        example = archive[-1]["path"].split("/")[-1]
        with netCDF4.Dataset(AMEDAS_SRC / archive[-1]["path"]) as ds:
            n_stations = ds.dimensions["station"].size
            for name, v in ds.variables.items():
                if name in ("station_id", "time") or name.endswith("_q"):
                    continue
                variables.append({"name": name, "units": getattr(v, "units", ""),
                                  "scale": f"{float(getattr(v, 'scale_factor', 1.0)):g}"})

    index = {
        "dataset": "JMA AMeDAS 10-minute observations — half-month archive (NetCDF-4)",
        "attribution": meta.get("attribution_archive", "出典: 気象庁ホームページ"),
        "license": "公共データ利用規約（第1.0版）に準拠（気象庁ホームページのコンテンツ）。"
                   "利用時は出典と、編集・加工したデータであることを記載すること",
        "source_url": meta.get("source_url"),
        "slot_minutes": meta.get("slot_minutes", 10),
        "elements": meta.get("elements", []),
        "generated_at": meta.get("generated_at"),
        "archive": [{**a, "url": f"/{AMEDAS_URL}/{a['path']}"} for a in archive],
        "hourly": [{**h, "url": f"/{AMEDAS_URL}/{h['path']}"}
                   for h in sorted(hourly.values(), key=lambda h: h["start"])],
        "missing_periods": [{"start": p["start"], "end": p["end"]}
                            for p in periods if not p["path"] and not p["pending"]],
        "daily": [{"url": f"/{AMEDAS_URL}/{d['path']}", "month": d["month"], "days": d["days"],
                   "bytes": d["bytes"]} for d in daily],
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    write(f"{AMEDAS_URL}/index.html", env.get_template("data/amedas.html").render(
        page_title="アメダス 10 分値アーカイブ（NetCDF）", nav_active="about",
        build_year=datetime.now().year, periods=periods, variables=variables,
        daily=daily, daily_vars=daily_vars,
        n_stations=f"{n_stations:,}" if n_stations else "1,290", example_file=example))
    print(f"  [data] {AMEDAS_URL}/: 半月 {len(archive)} 本・日別 {len(daily)} か月（今回写したもの {n_copied}）"
          f" / 欠け {sum(1 for p in periods if not p['path'] and not p['pending'])} 期間"
          f" / まとめる前 {sum(1 for p in periods if p.get('pending'))} 期間")


DAILY_DIST = BASE / "dist" / "daily"              # export_dist.py の出力
DAILY_URL = "Data/Daily"


def build_data_daily(env: Environment) -> None:
    """観測ストアの日別値（気温・降水・日照。1880 年〜）を /Data/Daily/ で配る。

    日別値は一番よく使われるデータ。export_dist.py が年ごと・地点ごとの NetCDF を
    dist/daily/ に作る。ストアが書き換わったとき（毎日の蓄積・月次の確定値置換の後）
    だけ書き出しを走らせ、10 分ごとの生成では写すだけにする。書き出しは中身が
    変わったファイルしか置き換えないので、Pages へ上がるのも変わった年・地点だけ。
    """
    import subprocess
    import sys
    store = BASE / "store" / "observations.nc"
    manifest = DAILY_DIST / "manifest.json"
    if store.is_file() and (not manifest.is_file()
                            or manifest.stat().st_mtime < store.stat().st_mtime):
        r = subprocess.run([sys.executable, str(BASE / "export_dist.py")],
                           capture_output=True, text=True, timeout=1800)
        for line in (r.stdout + r.stderr).splitlines()[-4:]:
            print(f"  {line}")
        if r.returncode != 0:
            print("  [data] 日別値の書き出しに失敗（前回の分で続ける）")
        else:
            manifest.touch()       # 中身が同じで置き換えなかった回も「ストアのこの版は済んだ」と記す
    if not manifest.is_file():
        return

    out = PUBLIC / DAILY_URL
    n_copied = 0
    files = [p for p in DAILY_DIST.rglob("*") if p.is_file() and not p.name.startswith(".")]
    for src in files:
        dst = out / src.relative_to(DAILY_DIST)
        if dst.is_file() and dst.stat().st_size == src.stat().st_size \
                and int(dst.stat().st_mtime) >= int(src.stat().st_mtime):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        n_copied += 1

    man = json.loads(manifest.read_text(encoding="utf-8"))
    st = json.loads((DAILY_DIST / "stations.json").read_text(encoding="utf-8"))["stations"]

    def fmt(n: int) -> str:
        return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} KB"

    years = [{**y, "size": fmt(y["bytes"])} for y in reversed(man["years"])]
    sizes = {s_["code"]: s_["bytes"] for s_ in man["stations"]}
    pref_of = {s_["prec_no"]: s_["pref"] for s_ in st if s_.get("prec_no") and s_.get("pref")}
    groups: dict[int, list] = {}
    for s_ in sorted(st, key=lambda x: (x.get("kana") or x.get("name") or "")):
        groups.setdefault(s_.get("prec_no") or 999, []).append(
            {**s_, "size": fmt(sizes.get(s_["code"], 0))})
    pref_groups_ = [{"name": pref_of.get(k, "府県の分からない地点"), "stations": v}
                    for k, v in sorted(groups.items())]
    write(f"{DAILY_URL}/index.html", env.get_template("data/daily.html").render(
        page_title="日別の観測データ（NetCDF）", nav_active="about",
        build_year=datetime.now().year, coverage=man["coverage"], years=years,
        groups=pref_groups_, n_stations=len(st),
        n_abolished=sum(1 for s_ in st if not s_.get("active")),
        total=fmt(sum(y["bytes"] for y in man["years"]))))
    print(f"  [data] {DAILY_URL}/: 年 {len(years)} 本・地点 {len(st)} 本（今回写したもの {n_copied}）")


def prune_station_pages(targets: list, stations: dict) -> None:
    """Stations/JP と Climate/Chart から、今の地点に当たらない古いページを片付ける。

    生成は古いファイルを消さないので、slug が変わると古い URL のページが残り、
    同じ中身が 2 つ公開され続ける（2026-10-05、小文字の slug から旧サイトの
    URL 名に揃えたときに起きる）。この 2 つのディレクトリの、index.html だけを
    持つ地点ディレクトリに限って消す。"""
    keep_of = {"Stations/JP": {slug for _, r, _, slug in targets if r["elements"].get("temp")},
               "Climate/Chart": {slug for _, _, _, slug in targets},
               # 実況の地点ページ（generate_status.py）は同じ slug、平年値の無い地点はアメダス番号
               "Status/Station": {slug for _, _, _, slug in targets}
                                 | {str(r.get("amedas")) for r in stations["stations"].values()}}
    n = 0
    for sub, keep in keep_of.items():
        root = PUBLIC / sub
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if d.is_dir() and d.name not in keep and \
                    {f.name for f in d.iterdir()} <= {"index.html"}:
                shutil.rmtree(d)
                n += 1
    # 雨温図の比較用データ（/data/climate/{slug}.json）も同じ slug 空間
    cdir = PUBLIC / "data" / "climate"
    if cdir.is_dir():
        for f in cdir.glob("*.json"):
            if f.name != "index.json" and f.stem not in keep_of["Climate/Chart"]:   # index は選択用の索引
                f.unlink()
                n += 1
    if n:
        print(f"  [seo] 今の地点に当たらない古い地点ページ・データ {n} 件を片付けました")


def legacy_redirects(targets: list) -> list[str]:
    """旧サイト（weather.time-j.net の WeatherCore）の URL を、新しいサイトの行き先へ送る。

    地点ページ・雨温図は旧 URL 名のまま作るので転送は要らない（climate_targets）。
    ここで送るのは、新しいサイトに同じ URL のページが無いもの:
      301 … 同じ中身のページが別の URL にある（入口・平年値の月・予報図・廃止地点）
      302 … まだ作っていない過去の年・月・日のページ。後で同じ URL のページを
            作ったとき、恒久の転送がブラウザや検索エンジンに残らないよう一時にする
    パターンは「1 区切り分の値がある」形（:year など）にする。/Summer/Ranking/* だと
    /Summer/Ranking/ 自身にも当たり、転送が回り続けうる。
    Pages の上限は固定 2,000 件・パターン付き 100 件。"""
    lines = [
        # 入口（旧サイトには区画のトップがあった）
        "/Summer /Summer/Ranking/ 301",
        "/Summer/ /Summer/Ranking/ 301",
        "/Winter /Winter/Ranking/ 301",
        "/Winter/ /Winter/Ranking/ 301",
        "/Temperature /Temperature/HighsMain/ 301",
        "/Temperature/ /Temperature/HighsMain/ 301",
        "/Summer/Nettaiya /Temperature/TodayLowsDec/ 301",
        # 予報図は扱わない（気象業務法）。予報はデスクトップアプリで
        "/Gfs /App/ 301",
    ]
    for m in range(1, 13):
        lines.append(f"/Monthly/Heinenti/{m:02d} /Monthly/Heinenti{m:02d}/ 301")
        lines.append(f"/Monthly/HeinentiL/{m:02d} /Monthly/Heinenti{m:02d}l/ 301")
    # 旧サイトの地点のうち、新しいサイトに地点ページが無いもの（廃止・平年値なし）
    legacy_path = BASE / "legacy" / "station_slugs.json"
    if legacy_path.is_file():
        leg = json.loads(legacy_path.read_text(encoding="utf-8"))
        have_cl = {slug for _, _, _, slug in targets}                    # 雨温図のある地点
        have_st = {slug for _, r, _, slug in targets if r["elements"].get("temp")}  # 地点ページ
        olds = set(leg["slugs"].values()) | {u["slug"] for u in leg.get("unmatched", [])}
        for slug in sorted(olds):
            if slug not in have_st:
                to = f"/Climate/Chart/{slug}/" if slug in have_cl else "/Stations/"
                lines.append(f"/Stations/JP/{slug} {to} 301")
            if slug not in have_cl:
                lines.append(f"/Climate/Chart/{slug} /Climate/ 301")
        # 旧サイトが自分のリンクで使っていた別名（akita・Tokyoold など）
        for alias, slug in leg.get("aliases", {}).items():
            st_to = (f"/Stations/JP/{slug}/" if slug in have_st else
                     f"/Climate/Chart/{slug}/" if slug in have_cl else "/Stations/")
            cl_to = f"/Climate/Chart/{slug}/" if slug in have_cl else "/Climate/"
            lines.append(f"/Stations/JP/{alias} {st_to} 301")
            lines.append(f"/Climate/Chart/{alias} {cl_to} 301")
    # まだ作っていない過去のページ（一時）。固定のもの
    lines += [
        "/Temperature/SummerMonth /Summer/Ranking/ 302",
        "/Temperature/SummerMonth/ /Summer/Ranking/ 302",
        "/Temperature/WinterMonth /Winter/Ranking/ 302",
        "/Temperature/WinterMonth/ /Winter/Ranking/ 302",
    ]
    lines += [f"/Summer/SummerMonth{y} /Summer/Ranking/ 302" for y in range(2010, date.today().year + 1)]
    # ここからパターン付き。Pages は固定のものを先に置く決まり。名前付きの置き場所
    # （:year）は転送先でも使わないと無効になる（2026-10-05、使っていなかった行が
    # すべて効かなかった）ので、元の値をクエリに付けて残す（ページ側は無視する）
    lines.append("/Gfs/* /App/ 301")
    for kind in ("Ranking", "SummerDayList", "Hottest", "HottestList"):
        lines.append(f"/Summer/{kind}/:year /Summer/{kind}/?year=:year 302")
    for kind in ("Ranking", "WinterDayList", "Coldest", "LowestList"):
        lines.append(f"/Winter/{kind}/:year /Winter/{kind}/?year=:year 302")
    lines += [
        "/Monthly/Monthly/:ym /Monthly/Latest/?ym=:ym 302",
        "/Monthly/MonthlyL/:ym /Monthly/Latest/?ym=:ym 302",
        "/Temperature/SummerDay/:day /Temperature/HighsList/?day=:day 302",
        "/Temperature/SummerMonth/:k/:m /Summer/Ranking/?k=:k&m=:m 302",
        "/Temperature/SummerMonth/:m /Summer/Ranking/?m=:m 302",
        "/Temperature/WinterMonth/:k/:m /Winter/Ranking/?k=:k&m=:m 302",
        "/Temperature/WinterMonth/:m /Winter/Ranking/?m=:m 302",
    ]
    def is_dyn(l: str) -> bool:
        return ":" in l.split()[0] or "*" in l.split()[0]
    first_dyn = next((i for i, l in enumerate(lines) if is_dyn(l)), len(lines))
    assert not any(not is_dyn(l) for l in lines[first_dyn:]), "固定の転送はパターン付きより前に置く"
    for l in lines:
        src, dst = l.split()[:2]
        for ph in re.findall(r":[A-Za-z]\w*", src):
            assert ph in dst, f"名前付きの置き場所 {ph} を転送先で使っていない: {l}"
    n_dyn = sum(1 for l in lines if is_dyn(l))
    assert len(lines) - n_dyn <= 2000 and n_dyn <= 100, "Pages の _redirects の上限を超える"
    print(f"  [seo] _redirects（旧サイトの URL: 固定 {len(lines) - n_dyn} 件・パターン {n_dyn} 件）")
    return lines


def build_seo(env: Environment, stations: dict) -> None:
    """sitemap.xml・_redirects（旧URL誘導）・404.html。Pages 移行のサイトインフラ。"""
    import os
    # 公開先は weather.time-j.net（いま旧システムが動いている。置き換えたらここ）
    origin = os.environ.get("WEATHER_SITE_ORIGIN", "https://weather.time-j.net")

    urls = ["/", "/Temperature/HighsMain/", "/Temperature/LowsMain/",
            "/Temperature/HighsList/", "/Temperature/LowsList/",
            "/Summer/Ranking/", "/Winter/LowestList/", "/Climate/",
            "/Stations/", "/Monthly/", "/Monthly/Latest/",
            "/Precipitation/", "/App/", "/App/Develop/", "/About/", f"/{AMEDAS_URL}/", f"/{DAILY_URL}/"]
    urls += [f"/Monthly/Heinenti{m:02d}{l}/" for m in range(1, 13) for l in ("", "l")]
    targets = climate_targets(stations)
    urls += [f"/Climate/Chart/{s}/" for _, _, _, s in targets]
    urls += [f"/Stations/JP/{s}/" for _, r, _, s in targets
             if r["elements"].get("temp")]
    today = date.today().isoformat()
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    xml += [f"<url><loc>{origin}{u}</loc><lastmod>{today}</lastmod></url>" for u in urls]
    xml.append("</urlset>")
    (PUBLIC / "sitemap.xml").write_text("\n".join(xml), encoding="utf-8")
    print(f"  [seo] sitemap.xml ({len(urls)} URL)")

    prune_station_pages(targets, stations)
    (PUBLIC / "_redirects").write_text("\n".join(legacy_redirects(targets)), encoding="utf-8")

    # 配布ファイルのヘッダ。NetCDF は形式を明示し、他所のページやツールからも
    # 読めるよう CORS を開ける。封入済みの半月分は変わらないので長めに持たせる
    (PUBLIC / "_headers").write_text("\n".join([
        f"/{AMEDAS_URL}/*",
        "  Access-Control-Allow-Origin: *",
        f"/{AMEDAS_URL}/archive/*",
        "  Content-Type: application/x-netcdf",
        "  Cache-Control: public, max-age=86400",
        f"/{AMEDAS_URL}/index.json",
        "  Cache-Control: public, max-age=600",
        f"/{DAILY_URL}/*",
        "  Access-Control-Allow-Origin: *",
        "",
    ]), encoding="utf-8")

    write("404.html",
          '<!doctype html><meta charset="utf-8"><title>404</title>'
          '<body style="font-family:sans-serif;text-align:center;padding:60px">'
          '<h1>ページが見つかりません</h1>'
          '<p><a href="/">個人開発気象統計 トップへ</a></p></body>')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="public/ を作り直す")
    args = ap.parse_args()

    if args.clean and PUBLIC.exists():
        shutil.rmtree(PUBLIC)
    PUBLIC.mkdir(parents=True, exist_ok=True)

    env = make_env()
    print("WeatherCore 静的サイトを生成中...")
    copy_assets()
    # 自前アセット（モダンテーマ CSS・JS）は wwwroot のコピーの後に重ねる
    for src in (BASE / "assets").glob("*.css"):
        dst = PUBLIC / "css" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for src in (BASE / "assets" / "js").glob("*.js"):
        dst = PUBLIC / "js" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    # 服装投票ビーコン（アクセスログ集計用の 1x1 透明 GIF。aggregate_votes.py 参照）
    (PUBLIC / "vote.gif").write_bytes(
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\x00\x00\x00"
        b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")

    today, meta = load_today()
    fc = load_forecast()
    stations = load_stations()
    climate_targets(stations)   # slug 表を先に確定（station_slug が全ページで使う）
    # テンプレートで固定の地点へリンクするとき用（国際地点番号 → 今の URL 名）。
    # URL 名を直書きすると、slug の決め方を変えたときにリンクが切れる
    by_intl = {r.get("intl"): r for r in stations["stations"].values() if r.get("intl")}
    env.globals["slug_by_intl"] = lambda code: station_slug(by_intl.get(code, {}))
    hist = History()
    try:
        build_highsmain(env, today, meta, fc, stations, hist)
        build_lowsmain(env, today, meta, fc, stations, hist)
        build_lists(env, today, meta, stations, hist)
        build_rankings(env, today, meta, stations)
        build_season_pages(env, meta, stations, hist)
        build_climate(env, stations)
        build_stations(env, stations, hist)
        build_monthly(env, stations, hist)
        build_precipitation(env, stations)
        build_app(env)
        build_about(env)
        build_data_amedas(env)
        build_data_daily(env)
        build_seo(env, stations)
        build_home(env, today, meta, fc, stations, hist)
    finally:
        hist.close()
    print("完了: public/ に出力しました")


if __name__ == "__main__":
    main()
