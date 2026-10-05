"""過去の日別値から、旧サイト（WeatherCore）にあった集計ページの中身を作る。

旧サイトの URL（/Temperature/SummerDay/a20180723 など）を受けて、その場で
集計する。静的に作ると組み合わせの数だけページが要る（日ごとだけで 4,000 枚超）。

データは export_dist.py の年ごとのファイル（dist/daily/years/{年}.nc）。
固定長の次元で書いた配布用のファイルなので、観測ストア（observations.nc）を
直接読むときの注意（libnetcdf の実寸越え読み）が要らず、書き込み中のストアとも
ぶつからない。ファイルが置き換わったら読み直す（mtime をキャッシュの鍵に含める）。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
DIST = BASE / "dist" / "daily"
PUBLIC = BASE / "public"

# 旧サイトの集計の種類（URL の記号）。値は (変数, 閾値, 見出し)
KINDS = {
    "a": ("tmax", 35.0, "猛暑日（最高気温が35℃以上）"),
    "b": ("tmax", 30.0, "真夏日（最高気温が30℃以上）"),
    "c": ("tavg", 30.0, "平均気温が30℃以上"),
    "d": ("tmin", 25.0, "最低気温が25℃以上"),
}
FIRST_YEAR = 2010            # 旧サイトの集計が始まる年（月の表の列）
MAX_RUN = 200                # 連続日数を数える上限（遡る日数）


class YearData:
    """1 年分の日別値。値は物理量（℃）で、欠測は NaN。

    資料不足値（品質 4 以下）の扱いは気象庁と同じにする。その日の地点の一覧には
    載せて「]」を付け（short）、月平均などの統計には使わない（stat）。旧サイトもこの
    分け方で、2018 年 7 月 23 日の地点一覧と、同じ月の京田辺の月平均が一致する。
    品質の無い値（毎時値から求めた直近の値など）は正常として扱う。"""

    def __init__(self, year: int, path: Path):
        import netCDF4
        with netCDF4.Dataset(path) as ds:
            self.year = year
            self.codes = [int(c) for c in ds["station_code"][:]]
            self.first = date(1870, 1, 1) + timedelta(days=int(ds["time"][0]))
            self.n_days = ds.dimensions["time"].size
            self.vals, self.short = {}, {}
            for v in ("tmax", "tmin", "tavg"):
                self.vals[v] = np.ma.filled(ds[v][:].astype(np.float64), np.nan)
                if f"{v}_q" in ds.variables:
                    q = np.ma.filled(ds[f"{v}_q"][:].astype(np.int16), -1)
                    self.short[v] = (q >= 0) & (q <= 4)
                else:
                    self.short[v] = np.zeros(self.vals[v].shape, dtype=bool)
        self._stat: dict[str, np.ndarray] = {}

    def stat(self, var: str) -> np.ndarray:
        """統計用の値（資料不足値を NaN にしたもの）。"""
        if var not in self._stat:
            a = self.vals[var].copy()
            a[self.short[var]] = np.nan
            self._stat[var] = a
        return self._stat[var]

    def col(self, var: str, d: date) -> np.ndarray | None:
        j = (d - self.first).days
        if not 0 <= j < self.n_days:
            return None
        return self.vals[var][:, j]

    def col_short(self, var: str, d: date) -> np.ndarray:
        return self.short[var][:, (d - self.first).days]


@lru_cache(maxsize=6)
def _load_year(year: int, mtime: float) -> YearData:
    return YearData(year, DIST / "years" / f"{year}.nc")


def year_data(year: int) -> YearData | None:
    p = DIST / "years" / f"{year}.nc"
    if not p.is_file():
        return None
    return _load_year(year, p.stat().st_mtime)


def last_year() -> int:
    years = sorted(int(p.stem) for p in (DIST / "years").glob("*.nc"))
    return years[-1] if years else date.today().year


@lru_cache(maxsize=2)
def _meta(mtime_st: float, mtime_slug: float) -> tuple[dict, dict]:
    st = json.loads((DIST / "stations.json").read_text(encoding="utf-8"))["stations"]
    # 府県番号 → 府県名は多数決（富士山は府県番号 49＝山梨県だが府県名は静岡県）
    votes = Counter((s["prec_no"], s["pref"]) for s in st if s.get("prec_no") and s.get("pref"))
    pref_of_prec = {}
    for (prec, pref), _ in votes.most_common():
        pref_of_prec.setdefault(prec, pref)
    meta = {}
    for s in st:
        pref = s.get("pref") or pref_of_prec.get(s.get("prec_no")) or ""
        meta[s["code"]] = {"name": s.get("name") or str(s["code"]), "pref": pref,
                           "prec_no": s.get("prec_no") or 99}
    slugs_p = PUBLIC / "data" / "slugs.json"
    slugs = json.loads(slugs_p.read_text(encoding="utf-8")) if slugs_p.is_file() else {}
    return meta, slugs.get("stations", {})


def meta() -> tuple[dict, dict]:
    """(地点番号 → {name, pref}, 地点番号(文字列) → 地点ページの URL 名)"""
    p1, p2 = DIST / "stations.json", PUBLIC / "data" / "slugs.json"
    return _meta(p1.stat().st_mtime, p2.stat().st_mtime if p2.is_file() else 0.0)


def _station(code: int, info: dict, slugs: dict) -> dict:
    m = info.get(code, {"name": str(code), "pref": "", "prec_no": 99})
    slug = slugs.get(str(code))
    return {"name": m["name"], "pref": m["pref"], "order": (m["prec_no"], code),
            "url": f"/Stations/JP/{slug}/" if slug else None}


def _ranked(rows: list[dict], key: str, tie: str | None = None) -> list[dict]:
    """値の大きい順に並べ、同じ値は同じ順位にする（1, 2, 2, 4 …）。
    同じ値の中は tie（丸める前の値など）の大きい順、その次に府県番号順・地点番号順
    （旧サイトと同じく北から）。"""
    rows.sort(key=lambda r: (-r[key], -r[tie] if tie else 0, r["order"]))
    prev, rank = None, 0
    for i, r in enumerate(rows, 1):
        if r[key] != prev:
            rank, prev = i, r[key]
        r["rank"] = rank
    return rows


def day_ranking(kind: str, d: date) -> dict | None:
    """その日に条件を満たした地点を、値の高い順と連続日数の多い順に。"""
    var, thr, title = KINDS[kind]
    yd = year_data(d.year)
    if yd is None:
        return None
    col = yd.col(var, d)
    if col is None or np.isnan(col).all():
        return None
    info, slugs = meta()
    codes = yd.codes            # 地点の並びはどの年のファイルでも同じ（export_dist.py）
    hit = np.nonzero(col >= thr)[0]
    short = yd.col_short(var, d)
    by_value = [{**_station(codes[i], info, slugs), "value": round(float(col[i]), 1),
                 "short": bool(short[i])} for i in hit]

    # 連続日数: その日から遡って条件を満たし続けた日数（年をまたいで数える）
    runs = np.zeros(len(codes), dtype=int)
    alive = col >= thr
    day = d
    for _ in range(MAX_RUN):
        if not alive.any():
            break
        runs += alive
        day -= timedelta(days=1)
        prev = year_data(day.year)
        c = prev.col(var, day) if prev else None
        if c is None:
            break
        alive = alive & (c >= thr)
    by_run = [{**_station(codes[i], info, slugs), "days": int(runs[i])}
              for i in hit if runs[i] >= 2]
    return {"kind": kind, "title": title, "date": d,
            "by_value": _ranked(by_value, "value"), "by_run": _ranked(by_run, "days")}


def month_table(kind: str, month: int) -> dict:
    """その月の、日ごと・年ごとの「条件を満たした地点数」。列は新しい年から。"""
    var, thr, title = KINDS[kind]
    years = list(range(last_year(), FIRST_YEAR - 1, -1))
    n_days = 31 if month in (1, 3, 5, 7, 8, 10, 12) else 30 if month != 2 else 29
    grid = []       # [日][年] = 地点数 or None
    for dd in range(1, n_days + 1):
        row = []
        for y in years:
            try:
                d = date(y, month, dd)
            except ValueError:
                row.append(None)
                continue
            yd = year_data(y)
            c = yd.col(var, d) if yd else None
            row.append(None if c is None or np.isnan(c).all() else int((c >= thr).sum()))
        grid.append(row)
    return {"kind": kind, "title": title, "month": month, "years": years, "grid": grid}


MONTH_TOP = 100             # 月のランキングの上位何位まで（旧サイトと同じ）
MIN_COVER = 0.8             # 月の平均に含める地点: その期間の 8 割以上の日に値がある


def month_ranking(high: bool, year: int, month: int) -> dict | None:
    """その月の日最高・日平均・日最低気温の平均の、高い順（high）か低い順の上位。

    旧 /Monthly/Monthly/{YYYYMM}（高い順）・/Monthly/MonthlyL/{YYYYMM}（低い順）。
    平均は日々の値の単純平均を小数 1 桁に丸め、丸めた値で順位を付ける。同じ順位の中は
    丸める前の平均の順（2018 年 7 月で旧サイトと一致）。表は高い順なら最高・平均・最低、
    低い順なら最低・平均・最高の順に並べる（旧サイトと同じ）。今月は昨日までで集計する。"""
    yd = year_data(year)
    if yd is None:
        return None
    first = date(year, month, 1)
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    last = min(last, date.today() - timedelta(days=1))
    if last < first:
        return None
    j0, j1 = (first - yd.first).days, (last - yd.first).days + 1
    if j0 < 0 or j1 > yd.n_days:
        return None
    info, slugs = meta()
    n_days = j1 - j0
    tables = []
    order = [("tmax", "日最高気温の平均"), ("tavg", "日平均気温の平均"), ("tmin", "日最低気温の平均")]
    for var, label in (order if high else order[::-1]):
        block = yd.stat(var)[:, j0:j1]
        n = (~np.isnan(block)).sum(axis=1)
        ok = n >= MIN_COVER * n_days
        if not ok.any():
            tables.append({"title": label, "rows": []})
            continue
        means = np.full(len(yd.codes), np.nan)
        means[ok] = np.nanmean(block[ok], axis=1)
        rows = [{**_station(yd.codes[i], info, slugs),
                 "value": round(float(means[i]) + (1e-9 if means[i] >= 0 else -1e-9), 1),
                 "raw": float(means[i])}
                for i in np.nonzero(ok)[0]]
        sign = 1 if high else -1
        for r in rows:
            r["value"], r["raw"] = sign * r["value"], sign * r["raw"]
        rows = _ranked(rows, "value", tie="raw")
        for r in rows:
            r["value"], r["raw"] = sign * r["value"], sign * r["raw"]
        tables.append({"title": label, "rows": [r for r in rows if r["rank"] <= MONTH_TOP]})
    if not any(t["rows"] for t in tables):
        return None
    return {"high": high, "year": year, "month": month, "first": first, "last": last,
            "tables": tables}
