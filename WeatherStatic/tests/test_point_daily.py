#!/usr/bin/env python3
"""地点別にしか無い要素の日別の記録（weatherlib/pointdaily.py）。

確かめること
  1. 翌日 0:00 のスロット（前日 24 時の確定値）を採る。0:10 の新しい日の値は使わない
  2. 0:00 が無い地点は、その日の最後のスロットで代え、as_of に時刻を残す
  3. map にある要素（temp など）は入れず、map に無い要素だけを拾う
  4. 起時は UTC → 日本時間
  5. 月ごとの NetCDF に正しく入る（倍率・品質・起時の分・as_of）
  6. 確定値が来るはずの時刻の前は作らず、作った日は作り直さない
"""
import json
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))
from weatherlib import pointdaily, pointstore  # noqa: E402


def e(gust, gdir, gt, tmax, tt, temp=20.0):
    return {"temp": [temp, 0], "wind": [2.0, 0], "prefNumber": 44,
            "gust": [gust, 0], "gustDirection": [gdir, 0], "gustTime": {"hour": gt[0], "minute": gt[1]},
            "maxTemp": [tmax, 0], "maxTempTime": {"hour": tt[0], "minute": tt[1]}}


def put(day: str, block: str, data: dict) -> None:
    d = pointstore.POINT / day / block
    d.mkdir(parents=True, exist_ok=True)
    (d / "44.json").write_text(json.dumps(data), encoding="utf-8")
    pointstore._load.cache_clear()


def main() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'OK' if cond else 'NG'} {name}")
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pointstore.POINT = root / "point"
        pointstore.EXTRA = root / "extra"
        pointstore.MAP = root / "map"
        pointdaily.OUT, pointdaily.DAILY = root, root / "daily"
        (root / "index.json").write_text(json.dumps(
            {"elements": ["temp", "wind", "weather", "snow"]}), encoding="utf-8")

        # 10/4 の夜と 10/5 の 0 時台。44132 は 0:00 あり、44136 は 0:00 が欠けた
        put("20261004", "21", {
            "44132": {"20261004235000": e(7.5, 3, (2, 44), 22.8, (1, 22))},
            "44136": {"20261004234000": e(6.0, 5, (3, 0), 21.0, (4, 0))},
        })
        put("20261004", "00", {   # 44141 は 2:50 で途切れた（日最大とは言えない）
            "44141": {"20261004025000": e(9.9, 7, (17, 30), 15.0, (17, 0))},
        })
        put("20261005", "00", {
            "44132": {"20261005000000": e(7.7, 3, (14, 55), 22.8, (1, 22)),   # 23:55 に更新された
                      "20261005001000": e(3.9, 15, (15, 5), 18.8, (15, 7))},  # 新しい日
            "44136": {"20261005001000": e(2.0, 1, (15, 3), 18.0, (15, 5))},
        })

        print("6a. 確定値が来るはずの時刻（翌日 1 時）の前で、0:00 も無い日は作らない")
        put_only = pointdaily.catch_up(datetime(2026, 10, 4, 23, 55))
        check("当日はまだ作らない", put_only == 0)

        print("1〜4. 10/5 0:30 に作る")
        n = pointdaily.catch_up(datetime(2026, 10, 5, 0, 30))
        rec = json.loads(pointdaily.daily_path(date(2026, 10, 4)).read_text())
        t, s = rec["stations"]["44132"], rec["stations"]["44136"]
        check("1 日分を作った", n == 1)
        check("0:00 の確定値（23:55 の瞬間風速 7.7）を採る", t["gust"] == [7.7, 0] and t["as_of"] == "24:00")
        check("起時 UTC 14:55 → 23:55", t["gustTime"] == "23:55")
        check("最高気温の起時 UTC 1:22 → 10:22", t["maxTempTime"] == "10:22")
        check("0:10 の新しい日の値（3.9）は使わない", t["gust"][0] != 3.9)
        check("0:00 が欠けた地点は最後のスロット 23:40 で代える",
              s["gust"] == [6.0, 0] and s["as_of"] == "23:40")
        check("23 時より前で途切れた地点（2:50 まで）は入れない", "44141" not in rec["stations"])
        check("map にある要素（temp・wind）は入れない", "temp" not in t and "wind" not in t)
        check("観測要素でない prefNumber は入れない", "prefNumber" not in t)
        check("要素の一覧", rec["elements"] == ["gust", "gustDirection", "maxTemp"])
        check("確定値の地点数", rec["complete"] == 1)

        print("5. 月ごとの NetCDF")
        import netCDF4
        with netCDF4.Dataset(pointdaily.DAILY / "2026" / "10.nc") as ds:
            ids = list(ds["station_id"][:])
            i, j = ids.index("44132"), ids.index("44136")
            check("地点 × 日（31 日）", ds["gust"].shape == (2, 31))
            check("倍率を戻すと 7.7 m/s", abs(float(ds["gust"][i, 3]) - 7.7) < 1e-6)
            check("風向は 16 方位の整数", int(ds["gustDirection"][i, 3]) == 3)
            check("起時は 0 時からの分（23:55 → 1435）", int(ds["gustTime"][i, 3]) == 1435)
            check("as_of: 確定 1440 / 代用 23:40 → 1420",
                  int(ds["as_of"][i, 3]) == 1440 and int(ds["as_of"][j, 3]) == 1420)
            check("記録の無い日は欠測", ds["gust"][i, 0] is netCDF4.default_fillvals or
                  bool(getattr(ds["gust"][i, 0], "mask", False)))

        print("6b. 作った日は作り直さない")
        check("2 回目は 0 日", pointdaily.catch_up(datetime(2026, 10, 5, 0, 40)) == 0)

    print("✓ ok" if ok else "✗ 日別の記録が壊れています")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
