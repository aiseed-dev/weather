"""過去の日別値から、旧サイト（WeatherCore）にあった集計ページの中身を作る。

旧サイトの URL（/temperature/summerday/a20180723 など）のページは、日付の数だけ
あり、静的に作ると組み合わせの数だけページが要る。そこでページは種類ごとに 1 枚の
枠（/history/day/ など）にし、中身は月ごとのデータ（public/data/history/）から
ブラウザで描く。_redirects の 200（URL はそのままで枠を返す）でつなぐ。
集計はここで済ませ、スクリプトは並べて表示するだけにする（export）。

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
# 日ごとのページ（その日の地点・月の日ごとの地点数）に入れない地点: 南鳥島（47991）と
# 富士山（47639）。離島と山頂は平地の暑さ・寒さの比較に入れない（旧サイトも南鳥島は無い）。
# 月平均のランキングと夏・冬のページには入れる
DAY_EXCLUDE = frozenset({47991, 47639})


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

    def day_col(self, var: str, d: date) -> np.ndarray | None:
        """日ごとのページ用の 1 日分。DAY_EXCLUDE の地点は NaN にする。"""
        c = self.col(var, d)
        if c is None:
            return None
        if not hasattr(self, "_day_mask"):
            self._day_mask = np.array([code in DAY_EXCLUDE for code in self.codes])
        c = c.copy()
        c[self._day_mask] = np.nan
        return c

    def col_short(self, var: str, d: date) -> np.ndarray:
        return self.short[var][:, (d - self.first).days]

    def day_vals(self, var: str) -> np.ndarray:
        """日ごとのページ用の 1 年分（DAY_EXCLUDE の地点を NaN にしたもの）。"""
        key = "day_" + var
        if key not in self._stat:
            a = self.vals[var].copy()
            a[[code in DAY_EXCLUDE for code in self.codes]] = np.nan
            self._stat[key] = a
        return self._stat[key]


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
    return {"code": code, "name": m["name"], "pref": m["pref"], "order": (m["prec_no"], code),
            "url": f"/stations/jp/{slug.lower()}/" if slug else None}


@lru_cache(maxsize=None)
def _carry(year: int, kind: str, mtime: float) -> np.ndarray | None:
    """year の大晦日までの連続日数（翌年へ持ち越す分）。年のファイルが無ければ None。"""
    yd = year_data(year)
    if yd is None:
        return None
    return year_runs(yd, kind)[:, -1]


def year_runs(yd: YearData, kind: str) -> np.ndarray:
    """1 年分の連続日数（地点 × 日）。その日を含めて、条件を満たし続けた日数。

    前年の大晦日から続けて数える（前年のファイルが無ければ 0 から）。欠測の日で途切れる。
    MAX_RUN で頭打ち。"""
    key = "runs_" + kind
    if key in yd._stat:
        return yd._stat[key]
    var, thr, _ = KINDS[kind]
    with np.errstate(invalid="ignore"):
        hit = yd.day_vals(var) >= thr
    p = DIST / "years" / f"{yd.year - 1}.nc"
    prev = _carry(yd.year - 1, kind, p.stat().st_mtime) if p.is_file() else None
    run = prev.astype(np.int32) if prev is not None else np.zeros(len(yd.codes), dtype=np.int32)
    out = np.zeros(hit.shape, dtype=np.int16)
    for j in range(hit.shape[1]):
        run = np.where(hit[:, j], np.minimum(run + 1, MAX_RUN), 0)
        out[:, j] = run
    yd._stat[key] = out
    return out


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
    col = yd.day_col(var, d)
    if col is None or np.isnan(col).all():
        return None
    info, slugs = meta()
    codes = yd.codes            # 地点の並びはどの年のファイルでも同じ（export_dist.py）
    hit = np.nonzero(col >= thr)[0]
    short = yd.col_short(var, d)
    by_value = [{**_station(codes[i], info, slugs), "value": round(float(col[i]), 1),
                 "short": bool(short[i])} for i in hit]
    # 連続日数: その日から遡って条件を満たし続けた日数（年をまたいで数える）
    runs = year_runs(yd, kind)[:, (d - yd.first).days]
    by_run = [{**_station(codes[i], info, slugs), "days": int(runs[i])}
              for i in hit if runs[i] >= 2]
    return {"kind": kind, "title": title, "date": d,
            "by_value": _ranked(by_value, "value"), "by_run": _ranked(by_run, "days")}


def month_tables() -> dict[tuple[str, int], dict]:
    """全種類・全月の「日ごと・年ごとの条件を満たした地点数」。列は新しい年から。

    {(種類, 月): {"kind", "title", "month", "years", "grid": [日][年] = 地点数 or None}}。
    年のファイルは 1 回ずつ読む。"""
    years = list(range(last_year(), FIRST_YEAR - 1, -1))
    out = {}
    for kind, (_, _, title) in KINDS.items():
        for m in range(1, 13):
            n_days = 31 if m in (1, 3, 5, 7, 8, 10, 12) else 30 if m != 2 else 29
            out[(kind, m)] = {"kind": kind, "title": title, "month": m, "years": years,
                              "grid": [[None] * len(years) for _ in range(n_days)]}
    for yi, y in enumerate(years):
        yd = year_data(y)
        if yd is None:
            continue
        for kind, (var, thr, _) in KINDS.items():
            vals = yd.day_vals(var)
            have = ~np.isnan(vals).all(axis=0)
            with np.errstate(invalid="ignore"):
                counts = (vals >= thr).sum(axis=0)
            for j in np.nonzero(have)[0]:
                d = yd.first + timedelta(days=int(j))
                if d.year == y:
                    out[(kind, d.month)]["grid"][d.day - 1][yi] = int(counts[j])
    return out


def month_table(kind: str, month: int) -> dict:
    """その月の、日ごと・年ごとの「条件を満たした地点数」。列は新しい年から。"""
    return month_tables()[(kind, month)]


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


# ---------------------------------------------------------------- 書き出し（public/data/history/）
#
#   {年}/{月 2 桁}.json  その月の日ごと・種類ごとの地点
#                        {"year", "month", "days": {"23": {"a": {"v": [[地点, 値×10(, 1)]…],
#                                                            "r": [[地点, 日数]…]}, …}, …}}
#                        v は値の高い順、r は連続日数の多い順（2 日以上）。同じ値の中は
#                        府県番号・地点番号の順。順位はスクリプトが付ける（同じ値は同じ順位）。
#                        3 つ目の 1 は資料不足値。データの無い日・種類は入れない
#   {年}/monthly.json    月の平均気温のランキング
#                        {"year", "months": {"7": {"first", "last", "high": [表×3], "low": [表×3]}}}
#                        表は [[地点, 平均×10]…]（高い順なら最高・平均・最低、低い順は逆）
#   table/{種類}{月}.json  月の日ごとの地点数 {"kind", "month", "years": […], "grid": [[…]…]}
#   stations.json        {"stations": {地点: [名前, 府県, 地点ページの URL 名 or null]}}

EXPORT_VERSION = 1          # 書き出す形を変えたら上げる（全部を書き直す）


def _order_pos(yd: YearData, info: dict) -> np.ndarray:
    """地点の並び順（府県番号・地点番号）での位置。同じ値の中の並びに使う。"""
    keys = [(info.get(c, {}).get("prec_no", 99), c) for c in yd.codes]
    pos = np.empty(len(keys), dtype=np.int32)
    pos[sorted(range(len(keys)), key=keys.__getitem__)] = np.arange(len(keys))
    return pos


def export_month(year: int, month: int) -> dict | None:
    """その月の日ごと・種類ごとの地点（形は上の説明）。データが 1 日も無ければ None。"""
    yd = year_data(year)
    if yd is None:
        return None
    info, _ = meta()
    pos = _order_pos(yd, info)
    days = {}
    d = date(year, month, 1)
    while d.month == month:
        j = (d - yd.first).days
        if 0 <= j < yd.n_days:
            entry = {}
            for kind, (var, thr, _) in KINDS.items():
                col = yd.day_vals(var)[:, j]
                if np.isnan(col).all():
                    continue
                with np.errstate(invalid="ignore"):
                    hit = np.nonzero(col >= thr)[0]
                v10 = np.rint(col[hit] * 10).astype(int)
                short = yd.short[var][hit, j]
                runs = year_runs(yd, kind)[hit, j]
                iv = sorted(range(len(hit)), key=lambda n: (-v10[n], pos[hit[n]]))
                ir = sorted((n for n in range(len(hit)) if runs[n] >= 2),
                            key=lambda n: (-runs[n], pos[hit[n]]))
                entry[kind] = {
                    "v": [[yd.codes[hit[n]], int(v10[n])] + ([1] if short[n] else []) for n in iv],
                    "r": [[yd.codes[hit[n]], int(runs[n])] for n in ir]}
            if entry:
                days[str(d.day)] = entry
        d += timedelta(days=1)
    return {"year": year, "month": month, "days": days} if days else None


def export_monthly(year: int) -> dict | None:
    """その年の月ごとの平均気温のランキング（形は上の説明）。"""
    months = {}
    for m in range(1, 13):
        hi, lo = month_ranking(True, year, m), month_ranking(False, year, m)
        if hi is None and lo is None:
            continue
        r = hi or lo
        months[str(m)] = {
            "first": r["first"].isoformat(), "last": r["last"].isoformat(),
            "high": [[[s["code"], int(round(s["value"] * 10))] for s in t["rows"]]
                     for t in (hi["tables"] if hi else [])],
            "low": [[[s["code"], int(round(s["value"] * 10))] for s in t["rows"]]
                    for t in (lo["tables"] if lo else [])]}
    return {"year": year, "months": months} if months else None


def export_stations(slugs: dict) -> dict:
    """地点番号 → [名前, 府県, 地点ページの URL 名]。slugs は地点番号(文字列) → URL 名。"""
    info, _ = meta()
    return {"stations": {str(c): [m["name"], m["pref"], (slugs.get(str(c)) or "").lower() or None]
                         for c, m in sorted(info.items())}}


def _dump(path: Path, obj) -> bool:
    """JSON を書く。中身が同じなら書かない（公開の差分を増やさない）。書いたら True。"""
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if path.is_file() and path.read_text(encoding="utf-8") == body:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
    return True


def export(out: Path, slugs: dict, state: Path, log=print) -> dict:
    """public/data/history/ を書く。変わった年だけ書き直す。

    年のファイル（dist/daily/years/）の大きさと更新時刻を state に控え、変わった年と
    その翌年（連続日数が年をまたぐ）を書き直す。今年は毎回（今月は昨日までで集計する）。
    EXPORT_VERSION が変われば全部。"""
    years = {int(p.stem): p for p in (DIST / "years").glob("*.nc")}
    sig = {str(y): [p.stat().st_size, p.stat().st_mtime_ns] for y, p in years.items()}
    try:
        old = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    full = old.get("version") != EXPORT_VERSION
    changed = {y for y in years if full or old.get("years", {}).get(str(y)) != sig[str(y)]}
    todo = sorted({y for y in years if y in changed or (y - 1) in changed or y == date.today().year})
    n_written = 0
    for y in todo:
        for m in range(1, 13):
            path = out / f"{y}" / f"{m:02d}.json"
            obj = export_month(y, m)
            if obj is None:
                if path.is_file():
                    path.unlink()
                continue
            n_written += _dump(path, obj)
        obj = export_monthly(y)
        if obj is not None:
            n_written += _dump(out / f"{y}" / "monthly.json", obj)
    if full or any(y >= FIRST_YEAR for y in todo):
        for (kind, m), tbl in month_tables().items():
            n_written += _dump(out / "table" / f"{kind}{m}.json", tbl)
    n_written += _dump(out / "stations.json", export_stations(slugs))
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"version": EXPORT_VERSION, "years": sig}), encoding="utf-8")
    log(f"  [history] data/history/: 集計した年 {len(todo)}（{'全部' if full else '変わった年と今年'}）"
        f"・書き換えたファイル {n_written}")
    return {"years": todo, "written": n_written}
