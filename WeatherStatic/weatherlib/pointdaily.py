"""地点別 10 分値にしか無い要素を、日別の記録として残す。

なぜ要るか
    地点別 10 分値（public_amedas/point/）は手元に 3 日、R2 に 30 日しか残さない。
    10 分値の長期の記録は map ミラーが半月ごとの NetCDF に残すが、map には
    最大瞬間風速（gust・gustDirection・gustTime）や日最高・最低気温の起時が無い。
    これらは日別の値なので、1 日 1 回の記録にすれば足りる。

値の取り方（2026-10-05 実測・東京 44132）
    gust・maxTemp・minTemp は「その日のここまでの値」で、起時は UTC の
    {"hour", "minute"}。**翌日 0:00 のスロットが前日 24 時の確定値**を持ち、
    0:10 で新しい日に戻る。23:50 はまだ確定ではない（最後の 10 分に記録が
    出れば変わる）。なので翌日 0:00 を採り、無ければその日の 23 時以降の最後の
    スロットで代える（地点ごとに as_of に何時の値かを残す）。それより前で
    途切れた地点は記録しない。途中までの最大を日最大として残すと誤解を招く。
    値は気象庁が 1 日を通して計算したものなので、こちらが途中から集め始めた
    日でも、夜まで取れていれば 1 日分として正しい。

要素の一覧は持たない
    「地点別にあって map に無いもの」をその都度拾う（map の要素は
    public_amedas/index.json の elements）。気象庁が要素を足しても追随する。

出力
    public_amedas/daily/{YYYY}/{YYYYMMDD}.json   1 日分（正本。消さない）
    public_amedas/daily/{YYYY}/{MM}.nc            月ごと（地点 × 日）。配布用
"""
from __future__ import annotations

import calendar
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from weatherlib import pointstore

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "public_amedas"
DAILY = OUT / "daily"

# 翌日 0:00 が無いとき、代わりに使ってよい最も早いスロット（HHMM）
FALLBACK_FROM = "2300"

ATTRIBUTION = ("出典: 気象庁ホームページ (https://www.jma.go.jp/) / 気象庁のデータを"
               "編集・加工したものです。編集・加工の責任は AIseed にあります。")
SOURCE_URL = "https://www.jma.go.jp/bosai/amedas/data/point/{amedas}/{YYYYMMDD}_{HH}.json"

# 既知の要素の格納の仕方（知らない要素は float32 でそのまま入れる）
KNOWN = {
    "gust": ("i2", 0.1, "m/s", "daily maximum instantaneous wind speed"),
    "gustDirection": ("i1", 1.0, "16-point", "direction of the daily maximum gust (1=NNE … 16=N, 0=calm)"),
    "maxTemp": ("i2", 0.1, "degC", "daily maximum temperature"),
    "minTemp": ("i2", 0.1, "degC", "daily minimum temperature"),
}


def log(msg: str) -> None:
    print(f"[daily] {msg}", flush=True)


def map_elements() -> set[str] | None:
    """map JSON にある要素（mirror の索引）。無ければ None（判断できない）。"""
    p = OUT / "index.json"
    try:
        return set(json.loads(p.read_text(encoding="utf-8"))["elements"])
    except (FileNotFoundError, KeyError, ValueError):
        return None


def daily_path(d: date) -> Path:
    return DAILY / f"{d:%Y}" / f"{d:%Y%m%d}.json"


def _jst(t) -> str | None:
    if isinstance(t, dict) and "hour" in t and "minute" in t:
        return f"{(int(t['hour']) + 9) % 24:02d}:{int(t['minute']):02d}"
    return None


def _entries_of(day: date, block: str | None) -> dict[str, dict[str, dict]]:
    """その日（のブロック）のエリア束を読み、{番号: {時刻: 値}} にする。"""
    out: dict[str, dict[str, dict]] = {}
    for p in pointstore.area_files(day, block):
        for amedas, series in pointstore.load_area(p).items():
            out.setdefault(amedas, {}).update(series)
    return out


def build_day(d: date, extra_keys: set[str]) -> dict | None:
    """1 日分の記録。手元に材料が無ければ None。

    extra_keys は「map に無い要素」。値の要素（[値, 品質]）と、起時（*Time）を拾う。
    """
    final_key = f"{d + timedelta(days=1):%Y%m%d}000000"
    final = _entries_of(d + timedelta(days=1), "00")
    same_day = _entries_of(d, None)
    stations: dict[str, dict] = {}
    for amedas in sorted(set(final) | set(same_day)):
        entry, as_of = None, None
        if final_key in final.get(amedas, {}):
            entry, as_of = final[amedas][final_key], "24:00"
        else:
            keys = sorted(k for k in same_day.get(amedas, {})
                          if k.startswith(f"{d:%Y%m%d}") and k[8:12] >= FALLBACK_FROM)
            if keys:
                entry, as_of = same_day[amedas][keys[-1]], f"{keys[-1][8:10]}:{keys[-1][10:12]}"
        if not entry:
            continue
        rec = {}
        for k, v in entry.items():
            if k not in extra_keys:
                continue
            if k.endswith("Time"):
                t = _jst(v)
                if t:
                    rec[k] = t
            elif isinstance(v, list) and len(v) >= 2 and v[0] is not None:
                rec[k] = [v[0], v[1]]
        if rec:
            rec["as_of"] = as_of
            stations[amedas] = rec
    if not stations:
        return None
    elements = sorted({k for r in stations.values() for k in r
                       if k != "as_of" and not k.endswith("Time")})
    return {
        "date": d.isoformat(),
        "attribution": ATTRIBUTION,
        "source_url": SOURCE_URL,
        "note": ("地点別 10 分値にしか無い要素の日別の値（気象庁の「その日のここまでの値」の"
                 "24 時の値）。起時は日本時間。as_of は何時の値か（24:00 が確定。それ以外は"
                 "24 時のスロットが取れず、23 時以降のその時刻までの値で代えたもの。23 時より"
                 "前で途切れた地点は入れない）。値は [値, 品質]（品質は気象庁の配信のまま、0 が正常）"),
        "elements": elements,
        "complete": sum(1 for r in stations.values() if r["as_of"] == "24:00"),
        "stations": stations,
    }


def write_month_nc(year: int, month: int) -> Path | None:
    """その月の日別 JSON を地点 × 日の NetCDF にまとめる（毎回作り直す）。

    次元は固定長にする（unlimited にすると libnetcdf の実寸越え読みの不具合を
    踏みうる。ncstore.normalize_extents 参照）。"""
    import netCDF4
    n_days = calendar.monthrange(year, month)[1]
    days = {}
    for k in range(n_days):
        p = daily_path(date(year, month, k + 1))
        if p.is_file():
            days[k] = json.loads(p.read_text(encoding="utf-8"))
    if not days:
        return None
    stations = sorted({a for d in days.values() for a in d["stations"]})
    sidx = {a: i for i, a in enumerate(stations)}
    elements = sorted({e for d in days.values() for e in d["elements"]})
    times = sorted({k for d in days.values() for r in d["stations"].values()
                    for k in r if k.endswith("Time")})

    out = DAILY / f"{year:04d}" / f"{month:02d}.nc"
    tmp = out.with_suffix(".nc.tmp")
    out.parent.mkdir(parents=True, exist_ok=True)
    common = dict(zlib=True, complevel=5, shuffle=True)
    with netCDF4.Dataset(tmp, "w", format="NETCDF4") as ds:
        ds.title = f"JMA AMeDAS daily values only in point data {year:04d}-{month:02d}"
        ds.source = "気象庁ホームページ (https://www.jma.go.jp/)"
        ds.attribution = ATTRIBUTION
        ds.source_url = SOURCE_URL
        ds.timezone = "Asia/Tokyo (JST, UTC+09:00)"
        ds.note = ("各日 24 時の「その日のここまでの値」。as_of が 1440 未満の地点は、24 時の"
                   "スロットが取れずその時刻までの値で代えたもの")
        ds.generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        ds.generated_by = "AIseed Weather / weatherlib/pointdaily.py"
        ds.createDimension("station", len(stations))
        ds.createDimension("day", n_days)
        ds.createVariable("station_id", str, ("station",))[:] = np.array(stations, dtype=object)
        v = ds.createVariable("day", "i4", ("day",))
        v.units = f"days since {year:04d}-{month:02d}-01"
        v[:] = np.arange(n_days, dtype=np.int32)

        def grid(fill, dtype):
            return np.full((len(stations), n_days), fill, dtype=dtype)

        for e in elements:
            typ, scale, units, desc = KNOWN.get(e, ("f4", None, "", e))
            if typ == "f4":
                val, fill = grid(np.nan, np.float32), np.float32(np.nan)
            else:
                fill = np.iinfo(np.int16 if typ == "i2" else np.int8).min
                val = grid(fill, np.int16 if typ == "i2" else np.int8)
            q = grid(-1, np.int8)
            for k, d in days.items():
                for a, r in d["stations"].items():
                    if e not in r:
                        continue
                    x, flag = r[e]
                    val[sidx[a], k] = x if typ == "f4" else int(round(float(x) / scale))
                    q[sidx[a], k] = flag
            var = ds.createVariable(e, typ, ("station", "day"), fill_value=fill, **common)
            var.long_name, var.units = desc, units
            if scale is not None and scale != 1.0:
                var.scale_factor = scale
            var.set_auto_maskandscale(False)
            var[:] = val
            qv = ds.createVariable(f"{e}_q", "i1", ("station", "day"), fill_value=np.int8(-1), **common)
            qv.long_name = f"JMA quality flag for {e} (0 = normal)"
            qv[:] = q
        for t in times:
            arr = grid(-1, np.int16)
            for k, d in days.items():
                for a, r in d["stations"].items():
                    if t in r:
                        h, m = r[t].split(":")
                        arr[sidx[a], k] = int(h) * 60 + int(m)
            var = ds.createVariable(t, "i2", ("station", "day"), fill_value=np.int16(-1), **common)
            var.long_name = f"time of {t[:-4]} (minutes from 00:00 JST)"
            var.units = "minutes"
            var[:] = arr
        asof = grid(-1, np.int16)
        for k, d in days.items():
            for a, r in d["stations"].items():
                h, m = r["as_of"].split(":")
                asof[sidx[a], k] = int(h) * 60 + int(m)
        var = ds.createVariable("as_of", "i2", ("station", "day"), fill_value=np.int16(-1), **common)
        var.long_name = "time the value is taken from (minutes from 00:00 JST; 1440 = final)"
        var[:] = asof
    tmp.replace(out)
    return out


def catch_up(now: datetime) -> int:
    """手元に材料があり、記録がまだ無い日を作る。作った日の月の NetCDF も作り直す。

    その日の確定値（翌日 0:00 のスロット）が手元にあるか、それが来るはずの時刻を
    過ぎていれば作る。来るはずの時刻の前なら待つ（10 分ごとに呼ばれるので次回）。
    戻り値は作った日数。"""
    extra_known = map_elements()
    if extra_known is None:
        log("public_amedas/index.json が無いので、map に無い要素を判断できません。見送ります")
        return 0
    if not pointstore.POINT.is_dir():
        return 0
    days = sorted(date(int(p.name[:4]), int(p.name[4:6]), int(p.name[6:8]))
                  for p in pointstore.POINT.iterdir()
                  if p.is_dir() and len(p.name) == 8 and p.name.isdigit())
    # 翌日の 00 ブロックしか無い日（取り戻しで 0 時台だけ取った日）も対象にする
    candidates = sorted({d - timedelta(days=1) for d in days} | set(days))
    made, months = 0, set()
    for d in candidates:
        if d >= now.date() or daily_path(d).is_file():
            continue
        final_ready = (d + timedelta(days=1)) in days and any(
            f"{d + timedelta(days=1):%Y%m%d}000000" in s
            for s in _entries_of(d + timedelta(days=1), "00").values())
        overdue = now >= datetime.combine(d + timedelta(days=1), datetime.min.time()) + timedelta(hours=1)
        if not (final_ready or overdue):
            continue
        # map に無い要素: 地点別のある 1 スロットの鍵から map の要素を引く
        rec = build_day(d, _extra_keys(d, extra_known))
        if rec is None:
            continue
        p = daily_path(d)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(p)
        log(f"{d}: {len(rec['stations'])} 地点（24 時の確定値 {rec['complete']}）"
            f" 要素 {', '.join(rec['elements'])}")
        made += 1
        months.add((d.year, d.month))
    for y, m in sorted(months):
        out = write_month_nc(y, m)
        if out:
            log(f"{out.relative_to(OUT)} を作り直しました（{out.stat().st_size / 1024:.0f} KB）")
    return made


def _extra_keys(d: date, map_known: set[str]) -> set[str]:
    """その日の地点別データに出てくる鍵のうち、map に無いもの（観測要素でないものを除く）。"""
    # 欠測の要素はキーごと来ないので、1 地点や 1 エリアで決めず全地点から拾う
    keys: set[str] = set()
    for day, block in ((d + timedelta(days=1), "00"), (d, None)):
        for series in _entries_of(day, block).values():
            for e in series.values():
                keys |= set(e)
    return keys - map_known - pointstore.NOT_ELEMENTS
