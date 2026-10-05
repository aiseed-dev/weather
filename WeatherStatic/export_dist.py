#!/usr/bin/env python3
"""観測ストアの日別値を、静的配信用の NetCDF に切り出す（/Data/Daily/ で配る）。

日別の最高・最低・平均気温、降水量、日照は、このサイトのデータで一番よく
使われる。気象庁の「過去の気象データ・ダウンロード」型の動的な切り出しの代わりに、
使われ方ごとにあらかじめ分けた NetCDF を静的に置けば、サーバー側の処理が要らない。

  dist/daily/
    years/{yyyy}.nc       全地点 × 1 年（年ごとの分析向け）
    stations/{code}.nc    1 地点 × 全期間（「地点を選んで期間指定」の代わり）
    stations.json         地点の一覧（コード・名前・緯度経度・データのある期間）
    manifest.json         ファイルの一覧（大きさ・sha256・期間）

全期間・全地点を 1 本にしたもの（以前の full/observations.nc）は作らない。
Pages の 1 ファイル上限（25 MiB）を超えるため。必要なら年ごとをつなげばよい。

対象は観測ストアの全行のうち値のある地点すべて。現行の地点表（master/stations.json）
には無い廃止地点（旧 WeatherCore のダンプにある阿蘇山・伊吹山など）も含める。
名前・緯度経度は master/stations.json → master/station_codes.json → SQLite の順に引く。

同じ内容なら同じバイト列になるよう、ファイルに生成時刻を入れない。書いたものが
既存と同じなら置き換えない（Pages へのアップロードが変わった年・地点だけで済む）。

読み出しは行単位（libnetcdf の実寸越え読みの不具合を踏まない読み方。
ncstore.normalize_extents 参照）。チャンクのキャッシュを大きく取り、同じチャンクを
何度も展開しないようにする。

使い方:
    python export_dist.py [--out dist/daily]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import warnings
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

import netCDF4 as nc
import numpy as np

from weatherlib.ncstore import DAILY_EPOCH, FILL, FILL_B

BASE = Path(__file__).resolve().parent
NC = BASE / "store" / "observations.nc"
SQLITE = BASE / "store" / "weather.sqlite"
MASTER = BASE / "master"

ATTRIBUTION = ("出典: 気象庁ホームページ (https://www.jma.go.jp/) / 気象庁のデータを"
               "編集・加工したものです。編集・加工の責任は AIseed にあります。")
LICENSE = ("公共データ利用規約（第1.0版）に準拠（気象庁ホームページのコンテンツ）。"
           "利用・再配布の際は出典と、編集・加工したデータであることを記載すること")

# (ストアの変数名, 配布での名前, 型, 倍率, 単位, 説明)
VARS = [
    ("tmax", "tmax", "i2", 0.1, "degC", "daily maximum temperature"),
    ("tmax_minutes", "tmax_time", "i2", None, "minutes", "time of daily maximum (minutes from 00:00 JST)"),
    ("tmax_q", "tmax_q", "i1", None, "", "JMA quality information for tmax (8 = normal)"),
    ("tmin", "tmin", "i2", 0.1, "degC", "daily minimum temperature"),
    ("tmin_minutes", "tmin_time", "i2", None, "minutes", "time of daily minimum (minutes from 00:00 JST)"),
    ("tmin_q", "tmin_q", "i1", None, "", "JMA quality information for tmin (8 = normal)"),
    ("tavg", "tavg", "i2", 0.1, "degC", "daily mean temperature"),
    ("tavg_q", "tavg_q", "i1", None, "",
     "JMA quality information for tavg (8 = normal; missing = computed here from hourly values)"),
    ("tavg_count", "tavg_count", "i1", None, "",
     "number of hourly values used when tavg was computed here from hourly values"),
    ("precip", "precip", "i2", 0.1, "mm", "daily precipitation"),
    ("precip_q", "precip_q", "i1", None, "", "JMA quality information for precip (8 = normal)"),
    ("precip_none", "precip_none", "i1", None, "", "1 = no precipitation observed (JMA flag)"),
    ("sun", "sun", "i2", 0.1, "h", "daily sunshine duration"),
]
MAIN = ("tmax", "tmin", "tavg", "precip")        # 「値のある地点・期間」を決める変数


def log(msg: str) -> None:
    print(f"[dist] {msg}", flush=True)


def station_meta(conn) -> dict[int, dict]:
    """code → 地点の情報。現行 → 廃止を含む一覧 → SQLite の順に埋める。"""
    meta: dict[int, dict] = {}
    for r in conn.execute("SELECT code, amedas, name FROM stations WHERE code IS NOT NULL"):
        meta[int(r[0])] = {"code": int(r[0]), "amedas": r[1], "name": r[2]}
    scp = MASTER / "station_codes.json"
    if scp.is_file():
        for e in json.loads(scp.read_text(encoding="utf-8"))["entries"]:
            try:
                code = int(e["block_no"])
            except (KeyError, ValueError):
                continue
            if code in meta:
                m = meta[code]
                for k in ("name", "kana", "lat", "lon", "alt", "prec_no"):
                    if m.get(k) is None and e.get(k) is not None:
                        m[k] = e[k]
                m.setdefault("active", e.get("active"))
                m.setdefault("end", e.get("end"))
    sp = MASTER / "stations.json"
    if sp.is_file():
        for code, r in json.loads(sp.read_text(encoding="utf-8"))["stations"].items():
            code = int(code)
            if code in meta:
                for k in ("name", "kana", "pref", "lat", "lon", "alt", "amedas"):
                    if r.get(k) is not None:
                        meta[code][k] = r[k]
                if (r.get("etrn") or {}).get("prec_no") is not None:
                    meta[code]["prec_no"] = r["etrn"]["prec_no"]
                meta[code]["active"] = True
    return meta


def write_if_changed(tmp: Path, path: Path) -> bool:
    """tmp が既存と同じなら捨てる。違えば置き換える。置き換えたら True。"""
    if path.is_file() and path.stat().st_size == tmp.stat().st_size:
        if hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(tmp.read_bytes()).digest():
            tmp.unlink()
            return False
    tmp.replace(path)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="観測ストアの日別値を配布用 NetCDF に切り出す")
    ap.add_argument("--out", default="dist/daily")
    args = ap.parse_args()
    out = BASE / args.out
    started = time.monotonic()

    conn = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
    rows_code = conn.execute(
        "SELECT row, code FROM stations WHERE code IS NOT NULL ORDER BY code").fetchall()
    meta = station_meta(conn)
    conn.close()

    src = nc.Dataset(NC)
    src.set_auto_mask(False)
    dates = np.asarray(src["date"][:])                   # 1 次元の読みは正しい
    have = np.nonzero(dates != -1)[0]
    if not len(have):
        log("データがありません")
        return 1
    j0, j1 = int(have.min()), int(have.max())
    n_days = j1 - j0 + 1
    rows = [r for r, _ in rows_code]

    data: dict[str, np.ndarray] = {}
    for name, _, typ, *_ in VARS:
        v = src[name]
        v.set_var_chunk_cache(size=512 * 1024 * 1024, nelems=8009, preemption=0.75)
        arr = np.empty((len(rows), n_days), dtype=np.int16 if typ == "i2" else np.int8)
        for k, r in enumerate(rows):
            arr[k] = v[r, j0:j1 + 1]                     # 1 行の読みは常に正しい
        data[name] = arr
    src.close()
    log(f"読み込み: {len(rows)} 行 × {n_days:,} 日（{time.monotonic() - started:.0f} 秒）")

    # 値のある地点と、その期間
    has = np.zeros((len(rows), n_days), dtype=bool)
    for name in MAIN:
        has |= data[name] != (FILL if data[name].dtype == np.int16 else FILL_B)
    keep = [k for k in range(len(rows)) if has[k].any()]
    codes = [int(rows_code[k][1]) for k in keep]
    span = {}
    for k, code in zip(keep, codes):
        idx = np.nonzero(has[k])[0]
        span[code] = (DAILY_EPOCH + timedelta(days=j0 + int(idx[0])),
                      DAILY_EPOCH + timedelta(days=j0 + int(idx[-1])))

    def write_nc(path: Path, sel: list[int], d0: int, d1: int, title: str) -> bool:
        """sel（data の行）× [d0, d1)（data の列）を書く。中身が同じなら置き換えない。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".nc.tmp")
        with nc.Dataset(tmp, "w", format="NETCDF4") as ds:
            ds.title = title
            ds.source = "気象庁ホームページ (https://www.jma.go.jp/)"
            ds.attribution = ATTRIBUTION
            ds.license = LICENSE
            ds.Conventions = "CF-1.10"
            ds.timezone = "Asia/Tokyo (JST, UTC+09:00)"
            first = DAILY_EPOCH + timedelta(days=j0 + d0)
            last = DAILY_EPOCH + timedelta(days=j0 + d1 - 1)
            ds.time_coverage = f"{first.isoformat()}..{last.isoformat()}"
            ds.note = ("値は気象庁の日別値（etrn の確定値・最新の気象データ CSV）。tavg_q が欠測の日の"
                       "tavg は当サイトが毎正時の気温から計算した値（tavg_count が使った時刻の数）")
            ds.createDimension("station", len(sel))
            ds.createDimension("time", d1 - d0)
            v = ds.createVariable("station_code", "i4", ("station",))
            v.long_name = "JMA station code (kansho: WMO-style 5 digits / AMeDAS: etrn 4 digits)"
            v[:] = np.array([int(rows_code[k][1]) for k in sel], dtype=np.int32)
            names = [meta.get(int(rows_code[k][1]), {}).get("name") or "" for k in sel]
            ds.createVariable("station_name", str, ("station",))[:] = np.array(names, dtype=object)
            for key, unit in (("lat", "degrees_north"), ("lon", "degrees_east"), ("alt", "m")):
                vals = [meta.get(int(rows_code[k][1]), {}).get(key) for k in sel]
                v = ds.createVariable(key, "f4", ("station",), fill_value=np.float32(np.nan))
                v.units = unit
                v[:] = np.array([np.nan if x is None else float(x) for x in vals], dtype=np.float32)
            v = ds.createVariable("time", "i4", ("time",))
            v.units = f"days since {DAILY_EPOCH.isoformat()}"
            v[:] = np.arange(j0 + d0, j0 + d1, dtype=np.int32)
            chunk = (min(256, len(sel)), min(366, d1 - d0))
            for name, out_name, typ, scale, unit, desc in VARS:
                fill = FILL if typ == "i2" else FILL_B
                var = ds.createVariable(out_name, typ, ("station", "time"), fill_value=fill,
                                        zlib=True, complevel=5, shuffle=True, chunksizes=chunk)
                var.set_auto_maskandscale(False)        # 整数のまま素通しで置く
                var.long_name = desc
                if unit:
                    var.units = unit
                if scale is not None:
                    var.scale_factor = scale
                var[:, :] = data[name][sel, d0:d1]
        return write_if_changed(tmp, path)

    manifest = {"coverage": f"{DAILY_EPOCH + timedelta(days=j0)}..{DAILY_EPOCH + timedelta(days=j1)}",
                "attribution": ATTRIBUTION, "license": LICENSE, "years": [], "stations": []}

    # 1. 年ごと（全地点 × 1 年。地点の並びは全年で同じ）
    y_first = (DAILY_EPOCH + timedelta(days=j0)).year
    y_last = (DAILY_EPOCH + timedelta(days=j1)).year
    n_new = 0
    for year in range(y_first, y_last + 1):
        d0 = max((date(year, 1, 1) - DAILY_EPOCH).days, j0) - j0
        d1 = min((date(year, 12, 31) - DAILY_EPOCH).days, j1) - j0 + 1
        p = out / "years" / f"{year}.nc"
        n_new += write_nc(p, keep, d0, d1, f"Daily surface observations in Japan {year}")
        n_st = int(has[keep, d0:d1].any(axis=1).sum())
        manifest["years"].append({"path": f"years/{year}.nc", "year": year, "stations": n_st,
                                  "bytes": p.stat().st_size,
                                  "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    log(f"years/: {y_first}〜{y_last}（置き換え {n_new}）")

    # 2. 地点ごと（1 地点 × その地点のデータのある期間）
    n_new = 0
    for k, code in zip(keep, codes):
        a, b = span[code]
        d0, d1 = (a - DAILY_EPOCH).days - j0, (b - DAILY_EPOCH).days - j0 + 1
        p = out / "stations" / f"{code}.nc"
        name = meta.get(code, {}).get("name") or str(code)
        n_new += write_nc(p, [k], d0, d1, f"Daily surface observations at {name} ({code})")
        manifest["stations"].append({"path": f"stations/{code}.nc", "code": code,
                                     "bytes": p.stat().st_size})
    log(f"stations/: {len(keep)} 地点（置き換え {n_new}）")

    # 3. 地点の一覧とファイルの一覧（中身が同じなら置き換えない）
    st_list = []
    for code in codes:
        m = meta.get(code, {})
        a, b = span[code]
        st_list.append({"code": code, "name": m.get("name"), "kana": m.get("kana"),
                        "amedas": m.get("amedas"), "pref": m.get("pref"),
                        "prec_no": int(m["prec_no"]) if m.get("prec_no") is not None else None,
                        "lat": m.get("lat"), "lon": m.get("lon"), "alt": m.get("alt"),
                        "active": bool(m.get("active")), "first": a.isoformat(),
                        "last": b.isoformat(), "path": f"stations/{code}.nc"})
    for fname, obj in (("stations.json", {"attribution": ATTRIBUTION, "stations": st_list}),
                       ("manifest.json", manifest)):
        tmp = out / f".{fname}.tmp"
        out.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        write_if_changed(tmp, out / fname)

    total = sum(y["bytes"] for y in manifest["years"]) + sum(s["bytes"] for s in manifest["stations"])
    log(f"完了: 年 {len(manifest['years'])} 本・地点 {len(keep)} 本 / 合計 {total / 1e6:.1f} MB "
        f"（{time.monotonic() - started:.0f} 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
