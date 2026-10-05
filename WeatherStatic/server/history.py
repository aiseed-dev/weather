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
    """1 年分の日別値。値は物理量（℃）で、欠測は NaN。"""

    def __init__(self, year: int, path: Path):
        import netCDF4
        with netCDF4.Dataset(path) as ds:
            self.year = year
            self.codes = [int(c) for c in ds["station_code"][:]]
            self.first = date(1870, 1, 1) + timedelta(days=int(ds["time"][0]))
            self.n_days = ds.dimensions["time"].size
            self.vals = {v: np.ma.filled(ds[v][:].astype(np.float64), np.nan)
                         for v in ("tmax", "tmin", "tavg")}

    def col(self, var: str, d: date) -> np.ndarray | None:
        j = (d - self.first).days
        if not 0 <= j < self.n_days:
            return None
        return self.vals[var][:, j]


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
    pref_of_prec = {s["prec_no"]: s["pref"] for s in st if s.get("prec_no") and s.get("pref")}
    meta = {}
    for s in st:
        pref = s.get("pref") or pref_of_prec.get(s.get("prec_no")) or ""
        meta[s["code"]] = {"name": s.get("name") or str(s["code"]), "pref": pref}
    slugs_p = PUBLIC / "data" / "slugs.json"
    slugs = json.loads(slugs_p.read_text(encoding="utf-8")) if slugs_p.is_file() else {}
    return meta, slugs.get("stations", {})


def meta() -> tuple[dict, dict]:
    """(地点番号 → {name, pref}, 地点番号(文字列) → 地点ページの URL 名)"""
    p1, p2 = DIST / "stations.json", PUBLIC / "data" / "slugs.json"
    return _meta(p1.stat().st_mtime, p2.stat().st_mtime if p2.is_file() else 0.0)


def _station(code: int, info: dict, slugs: dict) -> dict:
    m = info.get(code, {"name": str(code), "pref": ""})
    slug = slugs.get(str(code))
    return {"name": m["name"], "pref": m["pref"],
            "url": f"/Stations/JP/{slug}/" if slug else None}


def _ranked(rows: list[dict], key: str) -> list[dict]:
    """値の大きい順に並べ、同じ値は同じ順位にする（1, 2, 2, 4 …）。"""
    rows.sort(key=lambda r: -r[key])
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
    by_value = [{**_station(codes[i], info, slugs), "value": round(float(col[i]), 1)}
                for i in hit]

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
