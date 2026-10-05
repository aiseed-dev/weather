#!/usr/bin/env python3
"""サーバー側で作る過去のページの集計（server/history.py）。

確かめること
  1. その日に条件を満たした地点を、気温の高い順に並べる。同じ値は同じ順位で、
     その中は府県番号順（旧サイトと同じく北から）
     資料不足値もその日の一覧には載せる（印を付ける。気象庁の日々の一覧と同じ）
  2. 連続日数は年をまたいで数える（12/31 → 1/1）。2 日以上の地点だけ載せる
  3. 月の表は「日 × 年」の地点数で、データの無い日は空（None）
  4. 月の平均気温のランキングは丸めた値で順位を付け、同じ順位の中は丸める前の値の順。
     資料不足値（品質 4 以下）は使わない。
     低い順のページは表を最低・平均・最高の順に並べる（旧サイトと同じ）
  5. 日ごとのページに入れない地点（DAY_EXCLUDE）は、その日の地点にも月の表の数にも入らない
  6. 季節のページの府県のまとまりは府県名で決める（富士山は静岡県）
"""
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import netCDF4

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))
from server import history  # noqa: E402

CODES = [47662, 47626, 47401]            # 東京・熊谷・稚内（府県番号 44, 43, 11）


def write_year(root: Path, year: int, tmax_by_day: dict[date, list[float]],
               q4: dict[date, int] | None = None) -> None:
    """q4 は {日: 地点の位置}。その日・その地点の tmax を資料不足値（品質 4）にする。"""
    first = date(year, 1, 1)
    n = (date(year, 12, 31) - first).days + 1
    arr = np.full((len(CODES), n), -32768, dtype=np.int16)
    for d, vals in tmax_by_day.items():
        if d.year == year:
            arr[:, (d - first).days] = [int(round(v * 10)) for v in vals]
    p = root / "years" / f"{year}.nc"
    p.parent.mkdir(parents=True, exist_ok=True)
    with netCDF4.Dataset(p, "w") as ds:
        ds.createDimension("station", len(CODES))
        ds.createDimension("time", n)
        ds.createVariable("station_code", "i4", ("station",))[:] = CODES
        t = ds.createVariable("time", "i4", ("time",))
        t[:] = np.arange((first - date(1870, 1, 1)).days, (first - date(1870, 1, 1)).days + n)
        for v in ("tmax", "tmin", "tavg"):
            var = ds.createVariable(v, "i2", ("station", "time"), fill_value=np.int16(-32768))
            var.set_auto_maskandscale(False)
            var.scale_factor = 0.1
            var[:] = arr if v == "tmax" else np.full_like(arr, -32768)
        q = np.full((len(CODES), n), 8, dtype=np.int8)
        for d, i in (q4 or {}).items():
            q[i, (d - first).days] = 4
        ds.createVariable("tmax_q", "i1", ("station", "time"))[:] = q


def main() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'OK' if cond else 'NG'} {name}")
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        history.DIST, history.PUBLIC = root, root / "public"
        (root / "stations.json").write_text(json.dumps({"stations": [
            {"code": 47662, "name": "東京", "pref": "東京都", "prec_no": 44},
            {"code": 47626, "name": "熊谷", "pref": "埼玉県", "prec_no": 43},
            {"code": 47401, "name": "稚内", "pref": "北海道", "prec_no": 11}]}), encoding="utf-8")
        (root / "public" / "data").mkdir(parents=True)
        (root / "public" / "data" / "slugs.json").write_text(
            json.dumps({"stations": {"47662": "Tokyo"}}), encoding="utf-8")
        days = {date(2025, 12, 30): [36, 30, 10], date(2025, 12, 31): [36, 36, 10],
                date(2026, 1, 1): [36, 36, 36], date(2026, 1, 2): [20, 20, 20]}
        march = {date(2024, 3, 1) + timedelta(days=i): [30.0, 29.0, 30.0] for i in range(31)}
        march[date(2024, 3, 1)] = [31.2, 29.0, 30.0]      # 東京の平均 30.04 → 30.0
        march[date(2024, 3, 2)] = [0.0, 29.0, 30.0]       # 東京の資料不足値。使えば平均が下がる
        write_year(root, 2024, march, q4={date(2024, 3, 2): 0})
        write_year(root, 2025, days)
        write_year(root, 2026, days, q4={date(2026, 1, 1): 0})   # 東京の 1/1 は資料不足値
        history._load_year.cache_clear()
        history._meta.cache_clear()

        print("1. その日の地点")
        r = history.day_ranking("a", date(2026, 1, 1))
        names = [(s["rank"], s["name"]) for s in r["by_value"]]
        check("3 地点とも 36℃で同じ順位 1", [x[0] for x in names] == [1, 1, 1])
        check("同じ値の中は府県番号順（稚内 → 熊谷 → 東京）",
              [x[1] for x in names] == ["稚内", "熊谷", "東京"])
        check("資料不足値もその日の一覧には載せ、印を付ける",
              [s["short"] for s in r["by_value"]] == [False, False, True])
        check("地点ページのある地点だけリンク", r["by_value"][2]["url"] == "/Stations/JP/Tokyo/"
              and r["by_value"][0]["url"] is None)

        print("2. 連続日数（年をまたぐ）")
        runs = {s["name"]: s["days"] for s in r["by_run"]}
        check("東京は 12/30 から 3 日", runs.get("東京") == 3)
        check("熊谷は 12/31 から 2 日", runs.get("熊谷") == 2)
        check("稚内は 1 日だけなので載せない", "稚内" not in runs)
        check("条件を満たす地点が無い日は空の一覧",
              history.day_ranking("a", date(2026, 1, 2))["by_value"] == [])
        check("データの無い日は None", history.day_ranking("a", date(2026, 3, 1)) is None)

        print("3. 月の表")
        t = history.month_table("a", 12)
        y25 = t["years"].index(2025)
        check("12/30 は 1 地点、12/31 は 2 地点", t["grid"][29][y25] == 1 and t["grid"][30][y25] == 2)
        check("データの無い日は空", t["grid"][0][y25] is None)

        print("4. 月の平均気温のランキング")
        hi = history.month_ranking(True, 2024, 3)
        rows = [(s["rank"], s["name"], s["value"]) for s in hi["tables"][0]["rows"]]
        check("資料不足値は平均に入れない。同じ 30.0 は同じ順位で、丸める前の高い東京が先",
              rows == [(1, "東京", 30.0), (1, "稚内", 30.0), (3, "熊谷", 29.0)])
        lo = history.month_ranking(False, 2024, 3)
        check("低い順のページは最低・平均・最高の順",
              [x["title"] for x in lo["tables"]] == ["日最低気温の平均", "日平均気温の平均", "日最高気温の平均"])
        rows = [(s["rank"], s["name"]) for s in lo["tables"][2]["rows"]]
        check("低い順: 同じ 30.0 の中は丸める前の低い稚内が先",
              rows == [(1, "熊谷"), (2, "稚内"), (2, "東京")])

        print("5. 日ごとのページに入れない地点")
        saved = history.DAY_EXCLUDE
        history.DAY_EXCLUDE = frozenset({47401})            # 稚内を外してみる
        history._load_year.cache_clear()
        try:
            r = history.day_ranking("a", date(2026, 1, 1))
            check("その日の地点から外れる", [s["name"] for s in r["by_value"]] == ["熊谷", "東京"])
            t = history.month_table("a", 1)
            check("月の表の数からも外れる", t["grid"][0][t["years"].index(2026)] == 2)
            check("月平均のランキングには残る", any(s["name"] == "稚内" for s in
                  history.month_ranking(True, 2024, 3)["tables"][0]["rows"]))
        finally:
            history.DAY_EXCLUDE = saved
            history._load_year.cache_clear()

    print("6. 季節のページの府県のまとまり")
    import generate
    def rec(pref, prec, amedas, temp=True):
        return {"pref": pref, "name": amedas, "amedas": amedas, "row": 0,
                "etrn": {"prec_no": prec}, "elements": {"temp": temp}}
    st = {"stations": {"1": rec("山梨県", 49, "49001"), "2": rec("山梨県", 49, "49002"),
                       "47639": rec("静岡県", 49, "50066"), "3": rec("静岡県", 50, "50001"),
                       "4": rec("静岡県", 50, "50002", temp=False)}}
    got = [(r["amedas"], r["group_prec"]) for _, r in generate.season_stations(st, past=False)]
    check("富士山は静岡県（50）にまとめる。今季は気温を測っている地点だけ",
          got == [("49001", 49), ("49002", 49), ("50001", 50), ("50066", 50)])

    print("✓ ok" if ok else "✗ 過去のページの集計が壊れています")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
