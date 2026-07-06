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
import shutil
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from weatherlib.filters import FILTERS, bcolor
from weatherlib.season import is_season, is_summer
from weatherlib.stations import MAIN_STATIONS

BASE = Path(__file__).resolve().parent
TEMPLATES = BASE / "templates"
DATA = BASE / "data"
MASTER = BASE / "master"
STORE_NC = BASE / "store" / "observations.nc"
PUBLIC = BASE / "public"

# 静的アセットの取得元（旧サイト WeatherCore の wwwroot）。存在すればコピーする。
# リポジトリ移動に備えて環境変数 WEATHERCORE_WWWROOT で上書き可能。
import os as _os
_candidates = [
    Path(_os.environ["WEATHERCORE_WWWROOT"]) if "WEATHERCORE_WWWROOT" in _os.environ else None,
    BASE.parent / "WeatherCore" / "WeatherCore" / "wwwroot",
    BASE.parent.parent / "WeatherCore" / "WeatherCore" / "wwwroot",
]
WWWROOT = next((p for p in _candidates if p and p.is_dir()),
               BASE.parent / "WeatherCore" / "WeatherCore" / "wwwroot")
ASSET_PATHS = ["css", "javascripts", "Images", "favicon.ico", "robots.txt"]


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
    # CSS のキャッシュバスティング（内容ハッシュを ?v= に付ける）
    import hashlib
    css = BASE / "assets" / "site.css"
    env.globals["css_version"] = (
        hashlib.md5(css.read_bytes()).hexdigest()[:10] if css.exists() else "0")
    return env


def copy_assets() -> None:
    if not WWWROOT.is_dir():
        print(f"  [assets] スキップ: {WWWROOT} が見つかりません")
        return
    for rel in ASSET_PATHS:
        src = WWWROOT / rel
        dst = PUBLIC / rel
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    print("  [assets] css / javascripts / Images をコピーしました")


def write(path_rel: str, html: str) -> None:
    out = PUBLIC / path_rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"  [html] {path_rel}  ({len(html):,} bytes)")


def season_period(now: datetime) -> tuple[datetime, datetime]:
    """記録の集計期間（開始, 終了=昨日）。夏=1/1 から、冬=寒候年（前年 8/1）から。"""
    if is_summer(now) or now.month >= 8:
        start = datetime(now.year, 1, 1)
    else:
        start = datetime(now.year - 1, 8, 1)
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
        "page_title": "気温と雨量の統計のページ",
        "page_header": "気温と雨量の統計のページ",
        "build_year": now.year,
    }
    import os as _os
    html = env.get_template("home.html").render(
        vote_url=_os.environ.get("WEATHER_VOTE_URL", "/vote.gif"),**context)
    write("index.html", html)


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
    wy_start = datetime(now.year - 1, 8, 1) if now.month < 8 else datetime(now.year, 8, 1)
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
                     f"{wy_start.year}年8月1日からの観測記録を集計して、冬日（最低気温が0度未満）の日数、"
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
                     f"{wy_start.year}年8月1日からの観測記録を集計して、日最低気温、日平均気温、日最高気温の"
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


def climate_targets(stations: dict) -> list:
    """月別平年値（気温・降水量）が 12 か月そろっている地点と一意 slug。

    Climate（雨温図）と Stations/JP（観測所ページ）が同じ slug 空間を
    共有するための単一の真実。slug の衝突解決は挿入順に依存するので、
    候補集合と並び順をここで固定する。結果はプロセス内でキャッシュ。
    """
    global _CLIMATE_TARGETS
    if _CLIMATE_TARGETS is not None:
        return _CLIMATE_TARGETS
    targets = []
    slugs_seen: dict[str, int] = {}
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


def build_seo(env: Environment, stations: dict) -> None:
    """sitemap.xml・_redirects（旧URL誘導）・404.html。Pages 移行のサイトインフラ。"""
    import os
    origin = os.environ.get("WEATHER_SITE_ORIGIN", "https://creativeweb.jp")

    urls = ["/", "/Temperature/HighsMain/", "/Temperature/LowsMain/",
            "/Temperature/HighsList/", "/Temperature/LowsList/",
            "/Summer/Ranking/", "/Winter/LowestList/", "/Climate/",
            "/Stations/", "/Monthly/", "/Monthly/Latest/",
            "/Precipitation/", "/Forecast/"]
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

    # 旧URL → 新URL。大文字 place（旧 /Stations/JP/Tokyo）は静的に列挙
    lines = ["/Gfs/* /Forecast/ 301",
             "/Monthly/Heinenti/:m /Monthly/ 301",
             "/Monthly/Monthly/* /Monthly/Latest/ 301"]
    for _, rec, _, slug in targets:
        old = rec.get("place") or rec.get("en") or ""
        if old and old != slug:
            lines.append(f"/Stations/JP/{old} /Stations/JP/{slug}/ 301")
            lines.append(f"/Climate/Chart/{old} /Climate/Chart/{slug}/ 301")
    (PUBLIC / "_redirects").write_text("\n".join(lines[:2000]), encoding="utf-8")
    print(f"  [seo] _redirects ({min(len(lines), 2000)} 行)")

    write("404.html",
          '<!doctype html><meta charset="utf-8"><title>404</title>'
          '<body style="font-family:sans-serif;text-align:center;padding:60px">'
          '<h1>ページが見つかりません</h1>'
          '<p><a href="/">気温と雨量の統計 トップへ</a></p></body>')


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
        build_forecast(env)
        build_seo(env, stations)
        build_home(env, today, meta, fc, stations, hist)
    finally:
        hist.close()
    print("完了: public/ に出力しました")


if __name__ == "__main__":
    main()
